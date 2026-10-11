"""Server-owned original candidates and deterministic, exact crosschecks.

Supports narrowly labelled Chinese annual statement excerpts and explicit
internal diagnostic rows. No financial metadata is inferred from a title.
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from weakref import WeakKeyDictionary

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import DocumentProvenance, EvidenceRecord, Period
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.disclosures import DisclosureService
from investment_agent.evidence.original_parser import VERSION, parse_original
from investment_agent.evidence.queries import SnapshotQueryService
from investment_agent.evidence.snapshots import UNITS, digest
from investment_agent.investor.common import Contract, ObjectRef, Quantity
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot

HEADERS = {
    ("metric", "start", "end", "basis", "unit", "currency", "value"),
    ("指标", "开始日期", "截止日期", "口径", "单位", "币种", "数值"),
}
METRICS = {
    "营业收入": "revenue_total",
    "归属于母公司股东的净利润": "net_profit_parent",
    "净利润": "net_profit_total",
    "经营活动产生的现金流量净额": "operating_cash_flow",
}
SCALE = {"CNY": Decimal(1), "元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000)}
NUMBER = re.compile(
    r"(?:-?(?:0|[1-9]\d*)(?:\.\d+)?|-?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?|\((?:0|[1-9]\d*)(?:\.\d+)?\))"
)
PARSER_SLOTS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


class OriginalRequest(Contract):
    listing_id: int = Field(ge=1)
    original_id: UUID
    as_of: AwareDatetime
    offset: int = Field(default=0, ge=0, le=500)
    limit: int = Field(default=10, ge=1, le=20)


def numeric_rows(parsed: dict[str, Any], ticker: str) -> tuple[list[dict[str, Any]], list[str]]:
    fragments = parsed.get("fragments", [])
    warnings: list[str] = []
    identities = set()
    for fragment in fragments:
        identities.update(
            re.findall(r"(?:证券代码|Security code)\s*[:：]\s*(\d{6})(?!\d)", fragment["text"])
        )
    if identities != {ticker}:
        return [], ["original_security_identity_missing_or_conflicting"]
    # This first adapter intentionally handles a single statement excerpt,
    # not section linking in a whole report. Equal parent/consolidated values
    # cannot rescue ambiguous scope: reject the document before crosschecks.
    if len({f["table"] for f in fragments if "table" in f}) > 1 or any(
        "母公司" in f["text"] or "个别财务报表" in f["text"] for f in fragments
    ):
        return [], ["multiple_tables_or_parent_scope_needs_data"]
    tables: set[int] = set()
    ordinary_tables: set[int] = set()
    # Narrow realistic annual statement pattern: exact report range, explicit
    # consolidated scope and currency/unit, followed by 项目/本期/上期 cells.
    report_headers: list[dict[str, Any]] = []
    periods: set[tuple[str, str]] = set()
    money_units: set[str] = set()
    consolidated = False
    for header in fragments:
        if header.get("unsupported") or header.get("cells") is not None:
            continue
        text = header["text"]
        if re.fullmatch(r"(?:证券代码|Security code)\s*[:：]\s*" + re.escape(ticker), text):
            report_headers.append(header)
        match = re.fullmatch(
            r"报告期\s*[:：]\s*(\d{4})年1月1日\s*(?:至|—|-)\s*(\d{4})年12月31日", text
        )
        if match and match[1] == match[2]:
            periods.add((match[1] + "-01-01", match[2] + "-12-31"))
            report_headers.append(header)
        match = re.fullmatch(r"单位\s*[:：]\s*人民币(元|万元|亿元)", text)
        if match:
            money_units.add(match[1])
            report_headers.append(header)
        if text in {"合并利润表", "合并现金流量表", "财务口径：合并", "财务口径:合并"}:
            consolidated = True
            report_headers.append(header)
    # Bind the ordinary statement to its preceding explicit header block.
    # A later scope/period/unit, or an intervening unrelated section, cannot
    # retroactively describe an earlier table even when numeric values agree.
    first_table = next((i for i, f in enumerate(fragments) if "table" in f), None)
    ordinary_context = False
    if first_table is not None:
        header_positions = [i for i, f in enumerate(fragments) if f in report_headers]
        scope_positions = [
            i
            for i, f in enumerate(fragments)
            if f["text"] in {"合并利润表", "合并现金流量表", "财务口径：合并", "财务口径:合并"}
        ]
        ordinary_context = (
            len(scope_positions) == 1
            and all(i < first_table for i in header_positions)
            and scope_positions[0] < first_table
            and all(
                f in report_headers
                or re.fullmatch(
                    r"(?:公告编号|本公告更正原公告编号|更正目标公告ID)\s*[:：]\s*[A-Za-z0-9_-]+",
                    f["text"],
                )
                for f in fragments[scope_positions[0] : first_table]
            )
        )
    candidates = []
    for fragment in fragments:
        if fragment.get("unsupported"):
            continue
        cells = fragment.get("cells")
        row_scope = "consolidated" if consolidated else "unknown"
        if cells is not None:
            if tuple(cells) in {
                ("项目", "本期发生额", "上期发生额"),
                ("项目", "本期金额", "上期金额"),
            }:
                if (
                    len(periods) == 1
                    and len(money_units) == 1
                    and consolidated
                    and ordinary_context
                ):
                    ordinary_tables.add(fragment["table"])
                    report_headers.append(fragment)
                else:
                    warnings.append(
                        f"{fragment['locator']}:explicit_period_scope_currency_unit_required"
                    )
                continue
            if fragment["table"] in ordinary_tables:
                if len(cells) != 3:
                    warnings.append(f"{fragment['locator']}:ambiguous_numeric_row")
                    continue
                start, end = next(iter(periods))
                # The prior-year cell is preserved in the snippet, never assigned
                # a guessed prior period. Only explicitly bound current annual.
                cells = [
                    cells[0],
                    start,
                    end,
                    "period_to_date/annual",
                    next(iter(money_units)),
                    "人民币",
                    cells[1],
                ]
            if tuple(cells) in HEADERS:
                tables.add(fragment["table"])
                continue
            if fragment["table"] not in tables | ordinary_tables:
                continue
        else:
            # PDF numeric tables have no reliable semantic row structure. A
            # fully labelled record line is supported; flattened columns are not.
            explicit = re.fullmatch(
                r"(营业收入|归属于母公司股东的净利润|净利润|经营活动产生的现金流量净额)\s*[:：]\s*人民币([0-9.,()\-]{1,64})(元|万元|亿元)（合并口径，(\d{4})年1月1日至(\d{4})年12月31日）",
                fragment["text"],
            )
            if explicit and explicit[4] == explicit[5]:
                cells = [
                    explicit[1],
                    explicit[4] + "-01-01",
                    explicit[5] + "-12-31",
                    "period_to_date/annual",
                    explicit[3],
                    "人民币",
                    explicit[2],
                ]
                consolidated = True
                row_scope = "consolidated"
                report_headers.append(fragment)
            elif fragment["text"].startswith("Fact: "):
                cells = [c.strip() for c in fragment["text"][6:].split(" | ")]
            else:
                continue
        if len(cells) != 7:
            warnings.append(f"{fragment['locator']}:ambiguous_numeric_row")
            continue
        metric, start, end, basis, unit, currency, raw = cells
        metric = METRICS.get(metric, metric)
        if metric not in UNITS or metric in {
            "close",
            "pe_ttm",
            "pb_mrq",
            "ps_ttm",
            "turnover",
            "free_cash_flow",
        }:
            warnings.append(f"{fragment['locator']}:unsupported_metric")
            continue
        try:
            period = Period(
                start=date.fromisoformat(start), end=date.fromisoformat(end), basis=basis
            )
            if period.end > period.start and basis == "point_in_time":
                raise ValueError("invalid point period")
            if basis not in {
                "point_in_time",
                "period_to_date/annual",
                "period_to_date/q1",
                "period_to_date/semiannual",
                "period_to_date/q3",
                "trailing_twelve_months",
            }:
                raise ValueError("unsupported basis")
            if len(raw) > 64 or not NUMBER.fullmatch(raw):
                raise ValueError("ambiguous sign or numeric value")
            value = Decimal(raw.replace(",", "").strip("()"))
            if raw.startswith("("):
                value = -value
            if UNITS[metric] == "CNY":
                if currency not in {"CNY", "人民币"} or unit not in SCALE:
                    raise ValueError("explicit currency/unit required")
                with localcontext() as decimal_context:
                    decimal_context.prec = 80
                    quantity = Quantity(value=value * SCALE[unit], unit="CNY", currency="CNY")
            elif unit == UNITS[metric] and currency == "none":
                quantity = Quantity(value=value, unit=unit, currency=None)
            else:
                raise ValueError("unsupported unit")
        except (ValueError, InvalidOperation):
            warnings.append(f"{fragment['locator']}:period_basis_unit_currency_sign_needs_data")
            continue
        candidates.append(
            {
                "fragment": fragment,
                "metric": metric,
                "period": period,
                "quantity": quantity,
                "raw_unit": unit,
                "raw_currency": currency,
                "raw_value": raw,
                "reporting_scope": row_scope,
                "headers": list(report_headers),
            }
        )
    return candidates[:50], warnings


async def stage(
    session: AsyncSession,
    request: OriginalRequest,
    *,
    historical: bool = False,
    owner: str = "local_owner",
) -> tuple[dict[str, Any], bytes]:
    payload, body = await DisclosureService(session, owner).read(
        request.original_id, request.as_of, historical_reconstruction=historical
    )
    if (
        payload["listing_id"] != request.listing_id
        or payload["source"] != "cninfo.v1"
        or payload["exchange"] != "SZSE"
    ):
        raise DisclosureError("original_scope_mismatch")
    return payload, body


async def parse_staged(body: bytes, mime: str) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    gate = PARSER_SLOTS.setdefault(loop, asyncio.Semaphore(2))
    async with gate:
        pending = asyncio.create_task(asyncio.to_thread(parse_original, body, mime))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Keep the slot until the bounded child is reaped, even when an HTTP
            # caller disconnects. Cancellation cannot create unbounded orphans.
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            raise


def readback(
    payload: dict[str, Any], parsed: dict[str, Any], request: OriginalRequest
) -> dict[str, Any]:
    rows, warnings = numeric_rows(parsed, payload["ticker"].removesuffix(".SZ"))
    return {
        **parsed,
        "fragments": parsed.get("fragments", [])[request.offset : request.offset + request.limit],
        "fragment_count": len(parsed.get("fragments", [])),
        "warnings": [*parsed.get("warnings", []), *warnings],
        "original_id": payload["id"],
        "original_hash": payload["content_hash"],
        "published_at": payload["first_published_at"],
        "acquired_at": payload["acquired_at"],
        "available_from": payload["available_from"],
        "candidate_facts": [
            {
                "locator": r["fragment"]["locator"],
                "fragment_hash": r["fragment"]["content_hash"],
                "metric": r["metric"],
                "period": r["period"].model_dump(mode="json"),
                "value": r["quantity"].model_dump(mode="json"),
                "verification": "candidate",
            }
            for r in rows[request.offset : request.offset + request.limit]
        ],
        "candidate_fact_count": len(rows),
        "historical_reconstruction": False,
    }


async def save_candidates(
    session: AsyncSession,
    payload: dict[str, Any],
    body: bytes,
    parsed: dict[str, Any],
    request: OriginalRequest,
) -> dict[str, Any]:
    # Re-read exact owner/PIT/hash identity after parsing, inside the short final
    # transaction; the native boundary checks cancellation/settings separately.
    current, exact = await stage(session, request)
    if (
        current != payload
        or exact != body
        or hashlib.sha256(body).hexdigest() != payload["content_hash"]
    ):
        raise DisclosureError("original_integrity_failure")
    result = readback(payload, parsed, request)
    rows, _ = numeric_rows(parsed, payload["ticker"].removesuffix(".SZ"))
    result["fact_refs"] = []
    result["facts"] = []
    readable_after = datetime.fromisoformat(payload["available_from"])
    if parsed.get("status") != "ok":
        return result
    manifest_id = uuid5(
        NAMESPACE_URL,
        digest(
            {
                "original_id": payload["id"],
                "original_hash": payload["content_hash"],
                "parser": VERSION,
                "parsed": parsed,
            }
        ),
    )
    manifest = await session.get(SavedEvidence, (str(manifest_id), 1))
    if manifest is None:
        now = datetime.now(UTC)
        text = json.dumps(
            {**parsed, "original_hash": payload["content_hash"]}, ensure_ascii=False, sort_keys=True
        )
        saved_manifest = EvidenceRecord(
            id=manifest_id,
            revision=1,
            created_at=now,
            actor="original_parser",
            origin_ref=None,
            source_ref=ObjectRef(
                kind="disclosure_original",
                id=request.original_id,
                revision=1,
                field_path="parsed_fragments",
            ),
            source_kind="document",
            locator="parsed_fragments:" + VERSION,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            subject_ref=ObjectRef(kind="listing", id=request.listing_id, revision=1),
            period=None,
            published_at=datetime.fromisoformat(payload["first_published_at"]),
            acquired_at=datetime.fromisoformat(payload["acquired_at"]),
            available_from=now,
            verification="candidate",
            conflict="none",
            text=text,
        )
        await session.execute(
            insert(SavedEvidence)
            .values(
                id=str(manifest_id),
                revision=1,
                owner_id="local_owner",
                payload=saved_manifest.model_dump(mode="json"),
            )
            .on_conflict_do_nothing()
        )
    result["parse_artifact_ref"] = ObjectRef(
        kind="evidence", id=manifest_id, revision=1
    ).model_dump(mode="json")
    for row in rows:
        field = row["metric"]
        period = row["period"]
        quantity = row["quantity"]
        checks = await SnapshotQueryService(session).get_snapshot(
            payload["ticker"], field, request.as_of, report_period=period.end
        )
        agreeing = [
            c
            for c in checks.data or ()
            if c.verification == "verified"
            and c.conflict == "none"
            and c.period == period
            and c.value == quantity
            and max(c.acquired_at, c.available_from, c.published_at) <= request.as_of
        ]
        scoped = []
        for check in agreeing:
            snapshot = (
                await session.get(FinancialSnapshot, check.source_ref.id)
                if check.source_ref.kind == "financial_snapshot"
                else None
            )
            if (
                snapshot
                and snapshot.fields.get("reporting_scope")
                == row["reporting_scope"]
                == "consolidated"
                and snapshot.fields.get("currency") == "CNY"
            ):
                scoped.append(check)
        agreeing = scoped
        verified = checks.status == "ok" and bool(agreeing)
        crosschecks = tuple(c.source_ref for c in agreeing) if verified else ()
        fragment = row["fragment"]
        corrections, correction_fragment = (
            await verified_corrections(session, payload, parsed, row, request.as_of)
            if verified
            else ((), None)
        )
        # Verification is an immutable version. Re-parsing after a later exact
        # crosscheck produces another record, preserving the original candidate.
        identity = digest(
            {
                "original": payload["content_hash"],
                "original_id": payload["id"],
                "parser": VERSION,
                "locator": fragment["locator"],
                "metric": field,
                "period": period.model_dump(mode="json"),
                "value": quantity.model_dump(mode="json"),
                "crosschecks": [r.model_dump(mode="json") for r in crosschecks],
                "corrections": [r.model_dump(mode="json") for r in corrections],
            }
        )
        evidence_id = uuid5(NAMESPACE_URL, identity)
        existing = await session.get(SavedEvidence, (str(evidence_id), 1))
        if existing is None:
            now = datetime.now(UTC)
            record = EvidenceRecord(
                id=evidence_id,
                revision=1,
                created_at=now,
                actor="original_verifier" if verified else "original_parser",
                origin_ref=None,
                source_ref=ObjectRef(
                    kind="disclosure_original", id=request.original_id, revision=1, field_path=field
                ),
                source_kind="document",
                locator=fragment["locator"],
                content_hash=fragment["content_hash"],
                subject_ref=ObjectRef(kind="listing", id=request.listing_id, revision=1),
                period=period,
                published_at=datetime.fromisoformat(payload["first_published_at"]),
                acquired_at=datetime.fromisoformat(payload["acquired_at"]),
                available_from=max(datetime.fromisoformat(payload["available_from"]), now),
                verification="verified" if verified else "candidate",
                conflict="none",
                value=quantity,
                text=fragment["text"],
                correction_refs=corrections,
                document_provenance=DocumentProvenance(
                    parser_version=VERSION,
                    original_hash=payload["content_hash"],
                    fragment_hash=fragment["content_hash"],
                    metric=field,
                    raw_unit=row["raw_unit"],
                    raw_currency=row["raw_currency"],
                    raw_value=row["raw_value"],
                    crosscheck_refs=crosschecks,
                    verification_reason="exact_verified_snapshot_crosscheck"
                    if verified
                    else "exact_verified_crosscheck_unavailable",
                    reporting_scope=row["reporting_scope"],
                    header_locators=tuple(h["locator"] for h in row["headers"]),
                    header_hashes=tuple(h["content_hash"] for h in row["headers"]),
                    correction_locator=correction_fragment["locator"]
                    if correction_fragment
                    else None,
                    correction_fragment_hash=correction_fragment["content_hash"]
                    if correction_fragment
                    else None,
                ),
            )
            await session.execute(
                insert(SavedEvidence)
                .values(
                    id=str(evidence_id),
                    revision=1,
                    owner_id="local_owner",
                    payload=record.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
            existing = await session.get(SavedEvidence, (str(evidence_id), 1))
        assert existing is not None
        saved_record = EvidenceRecord.model_validate(existing.payload)
        readable_after = max(
            readable_after,
            saved_record.created_at,
            saved_record.available_from,
            saved_record.acquired_at,
            saved_record.published_at,
        )
        ref = ObjectRef(kind="evidence", id=evidence_id, revision=1)
        result["fact_refs"].append(ref.model_dump(mode="json"))
        if len(result["facts"]) < request.limit:
            result["facts"].append(existing.payload)
    # Parsing does not advance fixed task time. New records require a later
    # message to consume, just as newly acquired originals do in AG25.
    result["requires_new_message"] = readable_after > request.as_of
    result["readable_after"] = readable_after.isoformat()
    result["evidence_availability"] = (
        "new_parse_after_cutoff"
        if result["requires_new_message"]
        else "visible_at_cutoff"
        if rows
        else "no_supported_numeric_facts"
    )
    return result


def fact_identity(record: EvidenceRecord) -> tuple[Any, ...] | None:
    provenance = record.document_provenance
    if provenance is None or record.period is None or record.value is None:
        return None
    return (
        record.subject_ref,
        provenance.metric,
        record.period,
        record.value.unit,
        record.value.currency,
        provenance.reporting_scope,
    )


async def verified_corrections(
    session: AsyncSession,
    payload: dict[str, Any],
    parsed: dict[str, Any],
    row: dict[str, Any],
    cutoff: datetime,
) -> tuple[tuple[ObjectRef, ...], dict[str, Any] | None]:
    """Narrow explicit issuer statement + exact original and verified fact join.

    Titles, byte-version links and a model's claimed correction are insufficient.
    Only a standalone target declaration in the saved body is supported.
    """
    statements = []
    for fragment in parsed.get("fragments", []):
        match = re.fullmatch(
            r"(?:更正目标公告ID|Correction target disclosure ID|本公告更正原公告编号)\s*[:：]\s*([0-9]{1,30}|[0-9]{4}-[0-9]{1,6})",
            fragment["text"],
        )
        if match and not fragment.get("unsupported"):
            statements.append((match[1], fragment))
    if len(statements) != 1 or statements[0][0] == payload["disclosure_id"]:
        return (), None
    targets = (
        await session.scalars(
            select(DisclosureOriginal).where(
                DisclosureOriginal.owner_id == "local_owner",
                DisclosureOriginal.payload["listing_id"].as_integer() == payload["listing_id"],
                DisclosureOriginal.payload["disclosure_id"].as_string() == statements[0][0],
            )
        )
    ).all()
    if "-" in statements[0][0]:
        # Public issuer announcement numbers are distinct from CNInfo source
        # IDs. Resolve only from previously saved, hash-checked parse manifests
        # of this exact issuer; do not guess that a number is a source ID.
        targets = []
        manifests = (
            await session.scalars(
                select(SavedEvidence)
                .where(
                    SavedEvidence.owner_id == "local_owner",
                    SavedEvidence.payload["subject_ref"]["id"].as_integer()
                    == payload["listing_id"],
                    SavedEvidence.payload["source_ref"]["field_path"].as_string()
                    == "parsed_fragments",
                )
                .limit(101)
            )
        ).all()
        if len(manifests) > 100:
            return (), None
        for saved_manifest in manifests:
            record = EvidenceRecord.model_validate(saved_manifest.payload)
            if (
                record.text is None
                or max(
                    record.created_at,
                    record.acquired_at,
                    record.available_from,
                    record.published_at,
                )
                > cutoff
                or record.content_hash != hashlib.sha256(record.text.encode()).hexdigest()
            ):
                continue
            material = json.loads(record.text)
            declarations = [
                f
                for f in material.get("fragments", [])
                if re.fullmatch(r"公告编号\s*[:：]\s*" + re.escape(statements[0][0]), f["text"])
                and not f.get("unsupported")
            ]
            target_record = await session.get(DisclosureOriginal, str(record.source_ref.id))
            if (
                len(declarations) == 1
                and target_record is not None
                and target_record.owner_id == "local_owner"
                and material.get("original_hash") == target_record.content_hash
            ):
                targets.append(target_record)
    if not targets or len({(target.id, target.content_hash) for target in targets}) != 1:
        return (), None
    target = targets[0]
    if (
        target.payload["issuer_id"] != payload["issuer_id"]
        or target.payload["source"] != payload["source"]
        or datetime.fromisoformat(target.payload["first_published_at"])
        >= datetime.fromisoformat(payload["first_published_at"])
    ):
        return (), None
    await DisclosureService(session).read(UUID(target.id), cutoff)
    saved = (
        await session.scalars(
            select(SavedEvidence).where(
                SavedEvidence.owner_id == "local_owner",
                SavedEvidence.payload["source_ref"]["id"].as_string() == target.id,
            )
        )
    ).all()
    refs = []
    for item in saved:
        old = EvidenceRecord.model_validate(item.payload)
        if (
            old.verification == "verified"
            and old.document_provenance
            and old.document_provenance.metric == row["metric"]
            and old.document_provenance.reporting_scope == row["reporting_scope"]
            and old.period == row["period"]
            and old.value
            and old.value.unit == row["quantity"].unit
            and old.value.currency == row["quantity"].currency
            and old.value != row["quantity"]
            and max(old.created_at, old.available_from, old.acquired_at, old.published_at) <= cutoff
        ):
            refs.append(ObjectRef(kind="evidence", id=old.id, revision=old.revision))
    return tuple(refs), statements[0][1] if refs else None


async def document_limits(
    session: AsyncSession,
    record: EvidenceRecord,
    at: datetime,
    *,
    visible_at: datetime | None = None,
) -> list[str]:
    identity = fact_identity(record)
    if identity is None:
        return []
    rows = (
        await session.scalars(
            select(SavedEvidence).where(
                SavedEvidence.owner_id == "local_owner",
                SavedEvidence.payload["subject_ref"]["id"].as_integer() == record.subject_ref.id,
            )
        )
    ).all()
    limits = []
    for saved in rows:
        other = EvidenceRecord.model_validate(saved.payload)
        if (
            other.id == record.id
            or visible_at is not None
            and other.created_at > visible_at
            or max(other.created_at, other.published_at, other.acquired_at, other.available_from)
            > at
        ):
            continue
        if (
            fact_identity(other) == identity
            and other.value != record.value
            and not any(
                ref.id == other.id and ref.revision == other.revision
                for ref in record.correction_refs
            )
        ):
            limits.append(
                "critical_document_conflict"
                if other.verification == record.verification == "verified"
                else "critical_document_discrepancy_pending_verification"
            )
        if any(
            ref.id == record.id and ref.revision == record.revision for ref in other.correction_refs
        ):
            limits.append("source_corrected")
    return sorted(set(limits))
