"""Conservative, human-audited bank annual table checks (rule v2).

The audit locator is a claim to be re-derived from the stored PDF. It is not a
status token: verification always reloads the owner-scoped original and blob.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.originals import normalized
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)

BANK_ANNUAL_V2_RULE = "bank-annual-original/2"
BANKS: dict[str, str] = {
    "601398.SH": "工商银行",
    "601939.SH": "建设银行",
    "601288.SH": "农业银行",
    "600036.SH": "招商银行",
    "601166.SH": "兴业银行",
    "000001.SZ": "平安银行",
    "600919.SH": "江苏银行",
    "601009.SH": "南京银行",
    "002142.SZ": "宁波银行",
    "601128.SH": "常熟银行",
}
HEADERS: dict[str, tuple[str, ...]] = {
    "equity_parent": (
        "归属于母公司股东权益合计",
        "归属于母公司所有者权益合计",
        "归属于母公司股东的所有者权益合计",
        "归属于母公司股东权益",
        "归属于母公司所有者权益",
        "归属于母公司股东的权益",
        "归属于母公司股东的权益合计",
        "归属于本行股东权益合计",
    ),
    "roe_weighted_annual": ("加权平均净资产收益率", "加权平均ROE"),
    "net_interest_margin": ("净利息收益率", "净息差"),
    "npl_ratio": ("不良贷款率", "不良贷款比率", "集团贷款不良率"),
    "provision_coverage": ("拨备覆盖率", "集团拨备覆盖率"),
}
UNITS = {"CNY": Decimal(1), "CNY_thousand": Decimal(1000), "CNY_million": Decimal(1_000_000)}


def _text(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"bank v2 {key} is required")
    return value


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value).replace(",", "").replace("−", "-"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("bank v2 numeric value is invalid") from exc
    if not parsed.is_finite():
        raise ValueError("bank v2 numeric value is not finite")
    return parsed


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _numbers(text: str) -> list[Decimal]:
    return [
        _decimal(m.group())
        for m in re.finditer(r"(?<![\d.])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text)
    ]


def _validate_scope(page: str, proof: dict[str, Any], metric: str) -> None:
    """Only an explicit group table heading/default establishes group scope."""
    excerpt = normalized(_text(proof.get("locator_excerpt"), "locator_excerpt"))
    title = normalized(_text(proof.get("table_title"), "table_title"))
    if title not in excerpt:
        raise ValueError("table title is absent from the same-page locator")
    if metric == "equity_parent":
        if not any(
            x in excerpt for x in ("合并资产负债表", "合并及银行资产负债表", "合并及公司资产负债表")
        ):
            raise ValueError("equity requires a consolidated balance sheet heading")
    elif not any(x in excerpt for x in ("本集团", "集团口径", "集团财务指标", "合并口径")):
        scope = proof.get("scope_context")
        if not isinstance(scope, dict) or scope.get("kind") not in {
            "report_default",
            "section_default",
        }:
            raise ValueError("ratio has no explicit group scope or declared scope default")
        spage = scope.get("page")
        sexcerpt = scope.get("excerpt")
        if isinstance(spage, bool) or not isinstance(spage, int) or not isinstance(sexcerpt, str):
            raise ValueError("scope default locator is malformed")
        if scope.get("kind") == "section_default" and spage != proof.get("pdf_page"):
            raise ValueError("section scope default must be on the metric page")
        if scope.get("sha256") != _sha(sexcerpt) or normalized(sexcerpt) not in normalized(
            bank_page_text(proof["_body"], spage)
        ):
            raise ValueError("scope default is not present on its declared page")
        if not re.search(
            r"(?:除特别(?:注明|说明)外|若无特别说明).{0,60}(?:均为|为|按)?(?:本集团|本行及(?:本行所属|其)子公司合并数据|集团合并|合并口径|合并报表数据)",
            normalized(sexcerpt),
        ):
            raise ValueError("scope default does not explicitly set consolidated group scope")
    # Search only the actual table segment around title through the metric row.
    start = normalized(page).find(title)
    row = normalized(_text(proof.get("header"), "header"))
    at = normalized(page).find(row, start)
    segment = normalized(page)[start : at + len(row)] if start >= 0 and at >= start else excerpt
    if "本行" in segment and "本集团" not in segment and metric != "equity_parent":
        raise ValueError("metric row is explicitly standalone")
    note = normalized(_text(proof.get("definition_or_footnote"), "definition_or_footnote"))
    scope = proof.get("scope_context")
    scope_text = normalized(scope.get("excerpt", "")) if isinstance(scope, dict) else ""
    if note not in normalized(page) and note not in scope_text:
        raise ValueError("definition or footnote is not present in the cited PDF page")


def _check_locator(body: bytes, period: date, metric: str, proof: dict[str, Any]) -> None:
    if metric not in HEADERS:
        raise ValueError("unsupported bank annual metric")
    if proof.get("accounting_scope") != "group_consolidated":
        raise ValueError("bank annual field is not explicitly group-consolidated")
    page_no = proof.get("pdf_page")
    if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
        raise ValueError("PDF page is invalid")
    page = bank_page_text(body, page_no)
    layout = bank_page_text(body, page_no, layout=True)
    excerpt = _text(proof.get("locator_excerpt"), "locator_excerpt")
    if proof.get("locator_excerpt_sha256") != _sha(excerpt) or normalized(
        excerpt
    ) not in normalized(page):
        raise ValueError("locator excerpt/hash is not present on the declared PDF page")
    title = normalized(_text(proof.get("table_title"), "table_title"))
    header = _text(proof.get("header"), "header")
    hnorm = normalized(header)
    if header not in HEADERS[metric] or title not in normalized(excerpt):
        raise ValueError("table or metric label does not identify the requested field")
    years = [int(x) for x in re.findall(r"20\d{2}", normalized(proof.get("period_header", "")))]
    if not years or years[0] != period.year:
        raise ValueError("declared period header does not begin with report year")
    col = normalized(_text(proof.get("current_column"), "current_column"))
    if str(period.year) not in col or str(period.year - 1) in col:
        raise ValueError("current column is not the target year")
    if "本行" in col:
        raise ValueError("current column identifies the standalone bank")
    # The header, period and group words must occur in the actual layout stream.
    # A caller-provided selected-column label alone cannot establish semantics.
    layout_norm = normalized(layout)
    if str(period.year) not in layout_norm or hnorm not in layout_norm:
        raise ValueError("target year or metric is absent from actual table layout")
    current_column_parts = [part for part in col.split("/") if part]
    if normalized(_text(proof.get("period_header"), "period_header")) not in normalized(
        excerpt
    ) or any(part not in normalized(excerpt) for part in current_column_parts):
        raise ValueError("declared period/current column is absent from the locator excerpt")
    if (
        not any(token in normalized(excerpt) for token in ("本集团", "集团", "合并"))
        and metric != "equity_parent"
        and proof.get("scope_context") is None
    ):
        raise ValueError("group scope is not evidenced beside the metric table")
    if metric == "equity_parent":
        if not any(unit in normalized(excerpt) for unit in ("元", "千元", "百万元")):
            raise ValueError("equity unit is absent from locator")
        # Parallel group/own columns must be visible and in the expected order.
        flat = normalized(layout)
        if "本集团" in flat and "本行" in flat and flat.find("本集团") > flat.find("本行"):
            raise ValueError("group equity column follows standalone column")
    _validate_scope(page, {**proof, "_body": body}, metric)
    # Locate the row in layout and take only the first table-cell numeric value.
    # For multi-column rows an ambiguous extraction is refused; footnote text
    # after the row is never used as a value source.
    layout_lines = layout.splitlines()
    row_lines = []
    row_positions: list[int] = []
    for line_index, line in enumerate(layout_lines):
        line_norm = normalized(line)
        header_index = line_norm.find(hnorm)
        if header_index < 0 or line_norm[:header_index].strip():
            continue
        row_lines.append(line)
        row_positions.append(line_index)
    raw = _decimal(proof.get("raw_value"))
    if len(row_lines) != 1:
        raise ValueError("metric row is missing or repeated in table layout")
    row = row_lines[0]
    tail = row[row.find(header) + len(header) :]
    # A footnote marker attached to the row label is notation, not a cell.
    tail = re.sub(r"^\s*[（(]\s*\d{1,2}\s*[）)]", "", tail)
    values = _numbers(tail)
    if not values or values[0] != raw:
        raise ValueError("raw value is not the first value in the exact PDF metric row")
    title_index = next(
        (i for i, line in enumerate(layout_lines) if title in normalized(line)), None
    )
    if title_index is None:
        raise ValueError("table title is absent from actual layout")
    period_lines = [
        line
        for line in layout_lines[title_index + 1 : row_positions[0]]
        if len(re.findall(r"(?<!\d)20\d{2}(?:\s*年)?(?![\d,])", line)) >= 2
    ]
    if not period_lines:
        raise ValueError("actual table has no multi-year header before the metric row")
    actual_years = [
        int(year) for year in re.findall(r"(?<!\d)(20\d{2})(?:\s*年)?(?![\d,])", period_lines[-1])
    ]
    if actual_years[:2] != [period.year, period.year - 1]:
        raise ValueError(
            "actual table columns do not put the report year before the comparative year"
        )
    declared_years = [
        int(year)
        for year in re.findall(
            r"(?<!\d)(20\d{2})(?:\s*年(?:度)?)?(?!\d)",
            str(proof["period_header"]),
        )
    ]
    if declared_years[:2] != actual_years[:2]:
        raise ValueError("declared period columns differ from the actual table header")
    unit = proof.get("raw_unit")
    if metric == "equity_parent":
        factor = UNITS.get(str(unit))
        marker = {"CNY": "元", "CNY_thousand": "千元", "CNY_million": "百万元"}.get(str(unit))
        unit_text = normalized(excerpt)
        if (
            factor is None
            or marker is None
            or marker not in unit_text
            or (unit == "CNY" and any(other in unit_text for other in ("千元", "百万元")))
            or (unit == "CNY_thousand" and "百万元" in unit_text)
            or (unit == "CNY_million" and "千元" in unit_text)
        ):
            raise ValueError("equity unit does not match the printed table unit")
    else:
        if unit != "percent" or "%" not in excerpt and "％" not in excerpt:
            raise ValueError("ratio requires its printed percent unit")
        factor = Decimal("0.01")
    if _decimal(proof.get("parsed_value")) != raw * factor:
        raise ValueError("canonical value does not match printed unit conversion")


async def _load_original(
    session: AsyncSession,
    snapshot: Any,
    metric: str,
    proof: dict[str, Any],
    cutoff: datetime | None,
) -> tuple[bytes, dict[str, Any], str]:
    ref = ObjectRef.model_validate(proof["original_ref"])
    if ref.kind != "disclosure_original" or ref.revision != 1:
        raise ValueError("original reference is invalid")
    row = await session.get(DisclosureOriginal, str(ref.id))
    if (
        row is None
        or row.owner_id != "local_owner"
        or row.payload.get("listing_id") != snapshot.listing_id
    ):
        raise ValueError("original owner/listing scope mismatch")
    item = await session.get(DisclosureItem, row.item_id)
    if item is None or item.owner_id != "local_owner" or item.listing_id != snapshot.listing_id:
        raise ValueError("disclosure item owner/listing scope mismatch")
    from investment_agent.domain.a_share import format_listing_ticker
    from investment_agent.storage.models.reference import Listing

    listing = await session.get(Listing, snapshot.listing_id)
    if listing is None:
        raise ValueError("listing is missing")
    ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    if (
        ticker not in BANKS
        or row.payload.get("ticker") != ticker
        or item.payload.get("ticker") != ticker
        or item.payload.get("disclosure_id") != row.payload.get("disclosure_id")
    ):
        raise ValueError("stored original ticker does not match bank listing")
    if (
        proof.get("period") != snapshot.report_period.isoformat()
        or snapshot.report_period.day != 31
        or snapshot.report_period.month != 12
    ):
        raise ValueError("snapshot period is not the claimed annual period")
    identity = proof.get("report_identity")
    if (
        not isinstance(identity, dict)
        or identity.get("code") != ticker
        or identity.get("period") != snapshot.report_period.isoformat()
        or identity.get("disclosure_id") != row.payload.get("disclosure_id")
    ):
        raise ValueError("proof report identity differs from stored original")
    for key in ("source_url", "resolved_url", "title"):
        if not isinstance(row.payload.get(key), str) or not row.payload[key].strip():
            raise ValueError("stored source URL/title is missing")
    title = normalized(row.payload["title"])
    if not any(
        token in title
        for token in (
            f"{snapshot.report_period.year}年度报告",
            f"{snapshot.report_period.year}年年度报告",
        )
    ) or any(token in title for token in ("摘要", "英文", "H股", "取消")):
        raise ValueError("stored filing title is not the full target-year A-share annual report")
    blob = await session.get(DisclosureBlob, row.content_hash)
    if (
        blob is None
        or not blob.body.startswith(b"%PDF-")
        or hashlib.sha256(blob.body).hexdigest() != row.content_hash
        or proof.get("pdf_hash") != row.content_hash
    ):
        raise ValueError("stored PDF bytes/hash mismatch")
    cover = "".join(normalized(bank_page_text(blob.body, n)) for n in range(1, 9))
    front = bank_page_text(blob.body, 1)
    if (
        re.search(
            rf"(?<!\d){snapshot.report_period.year}\s*(?:年\s*|/\s*)?年\s*度\s*报\s*告", front
        )
        is None
    ):
        raise ValueError("the PDF cover year differs from the target annual period")
    if ticker[:6] not in cover and BANKS[ticker] not in cover:
        raise ValueError("annual-report PDF does not identify the listed bank")
    if (
        str(snapshot.report_period.year) not in cover
        or not any(term in cover for term in ("年度报告", "ANNUALREPORT"))
        or any(term in cover for term in ("摘要", "SUMMARY", "取消"))
    ):
        raise ValueError("PDF cover does not identify the full target-year annual report")
    if row.payload.get("source_url") != row.payload.get("resolved_url"):
        raise ValueError("stored source URL differs from the resolved original URL")
    source_url = row.payload["source_url"]
    if item.payload.get("source_url") != source_url or proof.get("source_url") != source_url:
        raise ValueError("source URL differs between audit, disclosure item, and original")
    from investment_agent.evidence.disclosure_http import validate_url

    validate_url(source_url, "original")
    audit = proof.get("audit_record")
    if (
        not isinstance(audit, dict)
        or audit.get("check_rule_version") != BANK_ANNUAL_V2_RULE
        or audit.get("status") != "verified"
    ):
        raise ValueError("human audit record is not v2 verified")
    audit_binding = (
        "metric",
        "pdf_page",
        "table_title",
        "period_header",
        "current_column",
        "header",
        "raw_value",
        "raw_unit",
        "parsed_value",
        "accounting_scope",
        "locator_excerpt",
        "locator_excerpt_sha256",
        "definition_or_footnote",
        "scope_context",
        "source_url",
    )
    if audit.get("metric") != metric or any(audit.get(k) != proof.get(k) for k in audit_binding):
        raise ValueError("audit record does not bind the field locator")
    if (
        audit.get("original_ref") != proof.get("original_ref")
        or audit.get("pdf_hash") != row.content_hash
    ):
        raise ValueError("audit record original reference/hash mismatch")
    captured = (
        snapshot.captured_at.replace(tzinfo=UTC)
        if snapshot.captured_at.tzinfo is None
        else snapshot.captured_at
    )
    boundary = min(captured, cutoff) if cutoff is not None else captured
    original_times = []
    for key in ("first_published_at", "acquired_at", "available_from"):
        stamp = datetime.fromisoformat(row.payload[key])
        if stamp.tzinfo is None or stamp > boundary:
            raise ValueError("original was unavailable by the evidence cutoff")
        original_times.append(stamp)
    if not original_times[0] <= original_times[1] <= original_times[2]:
        raise ValueError("original publication/acquisition/availability chronology is inconsistent")
    for key in ("checked_at",):
        stamp = datetime.fromisoformat(str(proof[key]))
        audit_stamp = datetime.fromisoformat(str(audit[key]))
        if (
            stamp.tzinfo is None
            or stamp != audit_stamp
            or not max(original_times) <= stamp <= boundary
        ):
            raise ValueError("audit time is future or inconsistent")
    if audit.get("acquired_at") != row.payload.get("acquired_at"):
        raise ValueError("audit acquisition time differs from stored original")
    if (
        proof.get("check_rule_version") != BANK_ANNUAL_V2_RULE
        or audit.get("report_identity") != identity
    ):
        raise ValueError("proof rule or audit report identity mismatch")
    return blob.body, row.payload, ticker


async def validate_bank_annual_v2(
    session: AsyncSession,
    snapshot: Any,
    metric: str,
    proof: dict[str, Any],
    *,
    cutoff: datetime | None = None,
) -> bool:
    """Re-read original bytes and validate a human-reviewed annual locator."""
    try:
        body, _, _ = await _load_original(session, snapshot, metric, proof, cutoff)
        _check_locator(body, snapshot.report_period, metric, proof)
        field = snapshot.fields[metric]
        parsed = _decimal(proof.get("parsed_value"))
        if field.get("value") is None or _decimal(field["value"]) != parsed:
            return False
        expected_start = (
            snapshot.report_period
            if metric == "equity_parent"
            else date(snapshot.report_period.year, 1, 1)
        )
        if (
            field.get("period", {}).get("start") != expected_start.isoformat()
            or field.get("period", {}).get("end") != snapshot.report_period.isoformat()
        ):
            return False
        if metric == "equity_parent":
            return bool(
                field.get("unit") == "CNY"
                and field.get("currency") == "CNY"
                and field.get("period", {}).get("basis") == "point_in_time"
            )
        return bool(
            field.get("unit") == "fraction"
            and field.get("currency") is None
            and field.get("period", {}).get("basis") == "annual_ratio"
        )
    except (KeyError, ValueError, TypeError, InvalidOperation, ImportError):
        return False


async def build_bank_annual_v2_field(
    session: AsyncSession,
    *,
    listing_id: int,
    report_period: date,
    audit: dict[str, Any],
    captured_at: datetime,
) -> tuple[PeerAnnualField, dict[str, Any]]:
    """Build a candidate field only from an already stored, explicitly audited PDF locator."""
    if (
        captured_at.tzinfo is None
        or captured_at.utcoffset() is None
        or captured_at > datetime.now(UTC)
    ):
        raise ValueError("bank annual capture time must be an actual aware observation")
    metric = _text(audit.get("metric"), "metric")
    if metric not in HEADERS:
        raise ValueError("unsupported bank annual metric")
    if audit.get("accounting_scope") != "group_consolidated":
        raise ValueError("bank annual field is not explicitly group-consolidated")
    if audit.get("status") != "verified" or audit.get("check_rule_version") != BANK_ANNUAL_V2_RULE:
        raise ValueError("only explicit v2 human audits can build fields")
    ref = ObjectRef.model_validate(audit["original_ref"])
    if ref.kind != "disclosure_original" or ref.revision != 1:
        raise ValueError("audit original reference is invalid")
    row = await session.get(DisclosureOriginal, str(ref.id))
    if row is None or row.owner_id != "local_owner" or row.payload.get("listing_id") != listing_id:
        raise ValueError("stored original owner/listing mismatch")
    blob = await session.get(DisclosureBlob, row.content_hash)
    if (
        blob is None
        or hashlib.sha256(blob.body).hexdigest() != row.content_hash
        or audit.get("pdf_hash") != row.content_hash
    ):
        raise ValueError("stored original bytes/hash mismatch")
    proof = dict(audit)
    proof.update(
        {
            "check_rule_version": BANK_ANNUAL_V2_RULE,
            "original_ref": audit["original_ref"],
            "pdf_hash": row.content_hash,
            "period": report_period.isoformat(),
            "report_identity": audit.get("report_identity"),
            "audit_record": dict(audit),
        }
    )
    snapshot_stub = SimpleNamespace(
        listing_id=listing_id,
        report_period=report_period,
        captured_at=captured_at,
    )
    body, _, _ = await _load_original(session, snapshot_stub, metric, proof, None)
    _check_locator(body, report_period, metric, proof)
    parsed = _decimal(audit.get("parsed_value"))
    is_equity = metric == "equity_parent"
    field = PeerAnnualField(
        metric=metric,
        value=parsed,
        unit="CNY" if is_equity else "fraction",
        currency="CNY" if is_equity else None,
        period=Period(
            start=report_period if is_equity else date(report_period.year, 1, 1),
            end=report_period,
            basis="point_in_time" if is_equity else "annual_ratio",
        ),
        source_field=audit["header"],
        raw_value=str(audit["raw_value"]),
        raw_unit=audit["raw_unit"],
        source_version=BANK_ANNUAL_V2_RULE,
        disclosed_on=date.fromisoformat(row.payload["first_published_at"][:10]),
        acquired_at=captured_at,
        verification=PeerFieldVerification.VERIFIED,
    )
    return field, proof
