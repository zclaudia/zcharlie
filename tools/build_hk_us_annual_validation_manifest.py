#!/usr/bin/env python3
"""Build the annual-validation manifest from reviewed rows and captured sources.

This tool never retrieves the network. Evidence bodies and reviewer-reviewed rows
are kept separately; the manifest only joins them with frozen fact records.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from annual_review_inline import inline_row_html

from investment_agent.materials.contracts import (
    AccountingStandard,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.equity_reconciliation import evaluate_equity_reconciliation
from investment_agent.materials.equity_scope_review import evaluate_equity_scope_review

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-10-15"
RAW = ROOT / "data/hk2-latest-evidence/2026-10-15/raw"
FIXTURES = ROOT / "tests/fixtures/materials/annual_validation"
MANIFEST = ROOT / "config/research_material/annual_validation_2026-10-15.json"
CURRENCY = {
    "clp-holdings": "HKD",
    "hsbc-holdings": "USD",
    "tencent-holdings": "CNY",
    "apple": "USD",
    "costco": "USD",
    "jpmorgan-chase": "USD",
    "exxon-mobil": "USD",
    "tsmc": "TWD",
}
STANDARD = {
    k: AccountingStandard.IFRS
    for k in ("clp-holdings", "hsbc-holdings", "tencent-holdings", "tsmc")
}
STANDARD["clp-holdings"] = AccountingStandard.HKFRS
STANDARD.update(
    {k: AccountingStandard.US_GAAP for k in ("apple", "costco", "jpmorgan-chase", "exxon-mobil")}
)
LATEST_PUBLISHED = {
    "clp-holdings": "2026-03-12",
    "hsbc-holdings": "2026-02-25",
    "tencent-holdings": "2026-04-09",
}
SEC_CIK = {
    "apple": "0000320193",
    "costco": "0000909832",
    "jpmorgan-chase": "0000019617",
    "exxon-mobil": "0000034088",
    "hsbc-holdings": "0001089113",
    "tsmc": "0001046179",
}
CATEGORY_METRIC = {
    "revenue": "revenue_operating",
    "profit": None,
    "assets": "total_assets",
    "equity": None,
    "operating_cash_flow": "operating_cash_flow",
    "capex": "capex_ppe",
}
_VISIBLE_CACHE: dict[str, str] = {}


def compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _capture_records() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    records = json.loads((RAW / "capture-index.json").read_text(encoding="utf-8"))["documents"]
    ok = [
        row
        for row in records
        if row.get("ok")
        and isinstance(row.get("http_status"), int)
        and 200 <= row["http_status"] < 300
    ]
    by_path = {row["path"]: row for row in ok}
    return ok, by_path


def _doc(
    record: dict[str, Any],
    *,
    doc_id: str | None = None,
    role: str | None = None,
    published_on: str | None = None,
) -> dict[str, Any]:
    return {
        "id": doc_id
        or f"{record['company_key']}:{record['document_role']}:{Path(record['path']).name}",
        "role": role or record["document_role"],
        "path": record["path"],
        "url": record["url"],
        "sha256": record["sha256"],
        "bytes": record["bytes"],
        "captured_at": record["captured_at"],
        "http_status": record["http_status"],
        "media_type": record.get("media_type") or "application/octet-stream",
        "published_on": published_on,
    }


def _root_clp_doc(path: str, company_key: str, published: str | None = None) -> dict[str, Any]:
    body_path = RAW / path
    meta = json.loads(
        body_path.with_suffix(body_path.suffix + ".meta.json").read_text(encoding="utf-8")
    )
    return {
        "id": f"{company_key}:issuer_annual:{body_path.name}",
        "role": "issuer_annual",
        "path": path,
        "url": meta["url"],
        "sha256": meta["sha256"],
        "bytes": meta["bytes"],
        "captured_at": meta["captured_at"],
        "http_status": meta["http_status"],
        "media_type": meta["media_type"],
        "published_on": published,
    }


def _root_capture_doc(
    path: str, company_key: str, doc_id: str, role: str, published: str | None = None
) -> dict[str, Any]:
    body_path = RAW / path
    meta = json.loads(
        body_path.with_suffix(body_path.suffix + ".meta.json").read_text(encoding="utf-8")
    )
    return {
        "id": doc_id,
        "role": role,
        "path": path,
        "url": meta["url"],
        "sha256": meta["sha256"],
        "bytes": meta["bytes"],
        "captured_at": meta["captured_at"],
        "http_status": meta["http_status"],
        "media_type": meta["media_type"],
        "published_on": published,
    }


def _row_locator(row: dict[str, Any]) -> dict[str, Any]:
    detail = row.get("locator_detail")
    if isinstance(detail, dict) and detail.get("kind") == "text" and detail.get("anchor"):
        return {"kind": "text", "anchor": detail["anchor"]}
    if isinstance(detail, dict) and detail.get("kind") == "html":
        return {"kind": "text", "anchor": detail.get("anchor_text") or row["source_label"]}
    structured = row.get("structured_locator")
    if isinstance(structured, dict):
        locator = dict(structured)
        if locator.get("kind") == "html":
            locator = {"kind": "text", "anchor": row["source_label"]}
        return locator
    match = re.search(r"PDF page\s*(\d+)", row.get("locator", ""), re.I)
    locator: dict[str, Any] = {"kind": "pdf", "page": int(match.group(1)) if match else 1}
    if row.get("source_label"):
        locator["row"] = row["source_label"]
    if row.get("selected_year"):
        locator["column"] = str(row["selected_year"])
    return locator


def _row_evidence_excerpt(row: dict[str, Any]) -> str:
    excerpt = row["excerpt"]
    return excerpt


def _make_reviewed_fact(
    row: dict[str, Any],
    doc: dict[str, Any],
    *,
    published: str | None = None,
    beginning: bool = False,
) -> dict[str, Any]:
    category = row["category"]
    end = date.fromisoformat(row["period_end"])
    start = date.fromisoformat(row["period_start"]) if row.get("period_start") else end
    if beginning:
        end = start - __import__("datetime").timedelta(days=1)
        start = end
        basis = PeriodBasis.INSTANT
        period = FactPeriod(start=None, end=end, basis=basis, label=f"FY{end.year}", days=None)
        category = "equity"
    elif category in {"assets", "equity"}:
        period = FactPeriod(
            start=None, end=end, basis=PeriodBasis.INSTANT, label=f"FY{end.year}", days=None
        )
    else:
        period = FactPeriod(
            start=start,
            end=end,
            basis=PeriodBasis.ANNUAL,
            label=f"FY{end.year}",
            days=(end - start).days,
        )
    metric = row.get("metric") or CATEGORY_METRIC[category]
    if category == "equity" and "IncludingPortionAttributableToNoncontrollingInterest" in row.get(
        "inline_xbrl", {}
    ).get("name", ""):
        metric = "total_equity"
    scope = EquityScope.UNKNOWN
    if category == "equity":
        if metric == "total_equity":
            scope = EquityScope.TOTAL
        else:
            scope = EquityScope.ORDINARY if metric == "equity_ordinary" else EquityScope.ALL_PARENT
    if category == "profit":
        scope = (
            EquityScope.ORDINARY
            if metric == "profit_ordinary_shareholders"
            else EquityScope.ALL_PARENT
        )
    locator = _row_locator(row)
    source_label = row["source_label"]
    raw_value = row.get("signed_value") or row["value"]
    scale = row.get("scale", 1)
    currency = row.get("currency", CURRENCY[row["company_key"]])
    if beginning:
        metric = row.get("metric", "equity_parent")
        if "IncludingPortionAttributableToNoncontrollingInterest" in row.get("inline_xbrl", {}).get(
            "name", ""
        ):
            metric = "total_equity"
            scope = EquityScope.TOTAL
    sign_convention = "as_reported"
    if category == "capex" and raw_value is not None:
        sign_convention = (
            "capital expenditure"
            if row["company_key"] == "clp-holdings"
            else "positive_outflow_magnitude"
        )
    f = FactRecord(
        metric=metric,
        period=period,
        semantics=FactSemantics(
            metric=metric,
            source_label=source_label,
            source_field=row.get("source_field", source_label),
            accounting_standard=STANDARD[row["company_key"]],
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=scope,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=(str(raw_value).replace(",", "") if raw_value is not None else None),
            raw_unit=row["raw_unit"],
            scale=scale,
            unit=currency,
            currency=currency,
            sign_convention=sign_convention,
        ),
        provenance=FactProvenance(
            source="issuer.annual_report",
            accession=row.get("accession"),
            original_hash=doc["sha256"],
            source_published_at=None,
            source_published_date=date.fromisoformat(published) if published else None,
            source_published_precision=DatePrecision.DATE if published else DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.VERIFIED,
        conflict="none",
        applicability="applicable",
        original_locator=compact(locator),
    )
    excerpt = _row_evidence_excerpt(row)
    if "html" in str(doc.get("media_type", "")).casefold():
        if row.get("inline_xbrl"):
            body = (RAW / doc["path"]).read_bytes()
            excerpt = _visible_text(inline_row_html(body, row))
        else:
            visible = _visible_for_doc(doc["path"])
            label = source_label
            indexes = [m.start() for m in re.finditer(re.escape(label), visible, re.IGNORECASE)]
            chosen = indexes[0] if indexes else 0
            excerpt = visible[max(0, chosen - 220) : chosen + 520] if indexes else visible[:700]
        locator = {"kind": "text", "anchor": excerpt}
        f = replace(f, original_locator=compact(locator))
    evidence = {
        "document_id": doc["id"],
        "locator": locator,
        "excerpt": excerpt,
        "source_label": source_label,
    }
    if raw_value is None:
        reason = (
            (row.get("locator_detail") or {}).get("reason")
            or "; ".join(row.get("notes", []))
            or "not applicable: no comparable industrial capex measure"
        )
        f = replace(
            f,
            applicability="not_applicable",
            value=FactValue(
                raw_value=None,
                raw_unit=row["raw_unit"],
                scale=scale,
                unit=currency,
                currency=currency,
                sign_convention="as_reported",
                missing_reason=reason,
            ),
        )
        evidence["review_reason"] = reason
    if row.get("column_years") is not None:
        evidence.update(
            {
                "column_years": [str(x) for x in row["column_years"]],
                "row_values": row["row_values"],
                "selected_year": str(row.get("selected_year", row["year"])),
            }
        )
    elif "pdf" in str(doc.get("media_type", "")).casefold():
        # Root-reviewed five-year summaries have newest-first values. Preserve
        # their column identity as parsed from the cited row, never as a guessed amount.
        source_excerpt = row.get("excerpt") or ""
        label_end = source_excerpt.casefold().find(source_label.casefold())
        number_context = (
            source_excerpt[label_end + len(source_label) :] if label_end >= 0 else source_excerpt
        )
        numeric = re.findall(r"(?<![A-Za-z])\(?-?\d[\d,]*(?:\.\d+)?\)?", number_context)
        numbers = [n.strip("()") for n in numeric if n.strip("()")]
        if len(numbers) >= 4:
            years = [2025, 2024, 2023, 2022, 2021][: len(numbers)]
            evidence.update(
                {
                    "column_years": [str(x) for x in years],
                    "row_values": numbers,
                    "selected_year": str(row["year"]),
                }
            )
    return {"category": category, "fact": fact_to_payload(f), "evidence": evidence}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def _index_link(index_doc: dict[str, Any], annual_doc: dict[str, Any]) -> str | None:
    body = (RAW / index_doc["path"]).read_bytes()
    p = _LinkParser()
    p.feed(body.decode("utf-8", errors="replace"))
    for link in p.links:
        if urljoin(index_doc["url"], link) == annual_doc["url"]:
            return link
    return None


def _company_docs(
    company: str, records: list[dict[str, Any]], by_path: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    docs: list[dict[str, Any]] = []
    ids: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec["company_key"] != company:
            continue
        doc_id = f"{company}:{rec['document_role']}:{rec['path']}"
        d = _doc(rec, doc_id=doc_id)
        if rec["document_role"] == "sec_shard":
            # The capture index keeps retry suffixes in local filenames; the
            # filename inside the official submissions index is canonical.
            # Materialize a byte-identical copy under that official name for
            # transparent, source-derived shard enumeration.
            name_match = re.search(r"CIK\d+-submissions-\d+\.json", rec["url"])
            if name_match:
                exact_path = Path("sec") / company / "selected-shards" / name_match.group(0)
                target = RAW / exact_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(RAW / rec["path"], target)
                d["path"] = exact_path.as_posix()
                ids[rec["path"]] = d
        if d["role"] == "submissions_index":
            d["id"] = f"{company}:submissions_index"
            doc_id = d["id"]
        if d["role"] == "sec_companyfacts":
            d["id"] = f"{company}:sec_companyfacts"
            doc_id = d["id"]
        if d["role"] == "official_index":
            d["id"] = f"{company}:official_index"
            doc_id = d["id"]
        docs.append(d)
        ids[rec["path"]] = d
    return docs, ids


def _review_rows() -> list[dict[str, Any]]:
    rows = []
    for name in (
        "hk-reviewed-facts.json",
        "tencent-reviewed-facts.json",
        "tsmc-reviewed-facts.json",
        "us-reviewed-facts.json",
        "us-history-reviewed-facts.json",
    ):
        p = FIXTURES / name
        if p.exists():
            rows.extend(json.loads(p.read_text(encoding="utf-8")))
    return rows


def _resolve_review_paths(rows: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    """Bind reviewer notes to the successful captured body, including retry paths."""
    for row in rows:
        want = row.get("raw_path")
        if want and (RAW / want).exists():
            # A base-name 4xx body may coexist with a successful .attempt-N body.
            exact = next((r for r in records if r.get("path") == want and r.get("ok")), None)
            if exact is not None:
                continue
        match = next(
            (
                r
                for r in records
                if r["company_key"] == row["company_key"]
                and r.get("url") == row.get("source_url")
                and r.get("ok")
            ),
            None,
        )
        if match is None:
            prefix = str(want or "").rsplit(".", 1)[0]
            match = next(
                (
                    r
                    for r in records
                    if r["company_key"] == row["company_key"]
                    and r.get("ok")
                    and str(r.get("path", "")).startswith(prefix + ".attempt-")
                ),
                None,
            )
        if match is not None:
            row["raw_path"] = match["path"]


def _close_equity_reviews(entry: dict[str, Any]) -> None:
    """Keep the earlier review intact beside explicitly audited replacements."""
    if entry["company_key"] == "hsbc-holdings":
        _close_hsbc_review(entry)
    elif entry["company_key"] == "costco":
        _close_costco_review(entry)


def _close_hsbc_review(entry: dict[str, Any]) -> None:
    review = json.loads((FIXTURES / "hsbc-restatement-reviewed-facts.json").read_text())
    doc = _root_capture_doc(
        review["source_document"]["raw_path"],
        "hsbc-holdings",
        "hsbc-holdings:restatement-annual-2023",
        "issuer_annual",
        "2024-02-21",
    )
    pub = review["publication_evidence"]
    index = _root_capture_doc(
        pub["document"]["raw_path"],
        "hsbc-holdings",
        "hsbc-holdings:restatement-publication-index",
        "official_index",
        "2024-02-21",
    )
    entry["documents"].extend([doc, index])
    entry["reconciliation_publication_evidence"] = {
        "document_id": index["id"],
        "locator": pub["locator"],
        "excerpt": pub["excerpt"],
        "published_on": "2024-02-21",
        "link_url": doc["url"].removesuffix("?download=1"),
    }
    balance_headers = [
        "ifrs17:2022-12-31",
        "ifrs17:2022-01-01",
        "ifrs4:2022-12-31",
        "ifrs4:2022-01-01",
    ]
    inputs = []

    def cited(identifier, value, basis, page, label, excerpt, values, headers, column):
        inputs.append(
            {
                "id": identifier,
                "value": str(value),
                "basis": basis,
                "scale": 1000000,
                "currency": "USD",
                "period_end": "2021-12-31",
                "source_ref": {
                    "document_id": doc["id"],
                    "sha256": doc["sha256"],
                    "source_label": label,
                    "published_on": "2024-02-21",
                    "excerpt": excerpt,
                    "locator": {
                        "kind": "pdf",
                        "page": page,
                        "row": label,
                        "column_index": column,
                        "column_values": values,
                        "column_headers": headers,
                        "selected_column": headers[column],
                    },
                },
            }
        )

    parent_label = "Total shareholders‘ equity"
    parent_excerpt = parent_label + " 177,833 189,015 187,484 198,250"
    parent_values = ["177833", "189015", "187484", "198250"]
    instruments_label = "Other equity instruments"
    instruments_excerpt = instruments_label + " 19,746 22,414 19,746 22,414"
    instruments_values = ["19746", "22414", "19746", "22414"]
    for prefix, basis, column in [("old", "ifrs4", 3), ("new", "ifrs17", 1)]:
        cited(
            prefix + "_parent",
            parent_values[column],
            basis,
            427,
            parent_label,
            parent_excerpt,
            parent_values,
            balance_headers,
            column,
        )
        cited(
            prefix + "_instruments",
            instruments_values[column],
            basis,
            427,
            instruments_label,
            instruments_excerpt,
            instruments_values,
            balance_headers,
            column,
        )
    ordinary_label = "Total ordinary shareholders’ equity"
    cited(
        "old_ordinary",
        "175836",
        "ifrs4",
        135,
        ordinary_label,
        ordinary_label + " 167,610 158,087 175,836",
        ["167610", "158087", "175836"],
        ["2023-12-31", "2022-12-31", "2021-12-31"],
        2,
    )
    transition_values = ["198250", "92738", "4558", "-99631", "-8847", "1947", "189015", "-9235"]
    transition_headers = [
        "ifrs4",
        "removal",
        "remeasurement",
        "fulfilment",
        "csm",
        "tax",
        "ifrs17",
        "movement",
    ]
    for column in range(1, 6):
        cited(
            f"adjustment_{column}",
            transition_values[column],
            "transition",
            424,
            "Total shareholders’ equity",
            review["transition_bridge"]["transition_table_excerpt"],
            transition_values,
            transition_headers,
            column,
        )
    calculation = evaluate_equity_reconciliation(
        {
            "method_version": "hsbc.ifrs17.ordinary_opening.v1",
            "inputs": inputs,
        }
    )
    entry["equity_reconciliations"] = [
        {key: calculation[key] for key in ("method_version", "inputs", "output", "calculation_ref")}
    ]
    first = entry["fiscal_years"][0]
    original = first["beginning_equity_fact"]
    entry["superseded_facts"] = [original]
    fact = dict(original["fact"])
    fact["value"] = {**fact["value"], "raw_value": calculation["output"]["value"]}
    fact["semantics"] = {
        **fact["semantics"],
        "source_label": "Derived IFRS 17 ordinary opening equity",
        "source_field": "IFRS 17 parent equity less other equity instruments",
    }
    fact["source_label"] = fact["semantics"]["source_label"]
    fact["source_field"] = fact["semantics"]["source_field"]
    fact["provenance"] = {
        **fact["provenance"],
        "source": "material.equity_reconciliation",
        "original_hash": calculation["input_hash"],
        "source_published_date": "2024-02-21",
        "source_published_precision": "date",
    }
    fact["original_locator"] = compact(
        {"kind": "calculation", "ref": calculation["calculation_ref"]}
    )
    first["beginning_equity_fact"] = {
        "category": "equity",
        "fact": fact,
        "calculation_ref": calculation["calculation_ref"],
        "evidence": {
            "kind": "derived_calculation",
            "calculation_ref": calculation["calculation_ref"],
            "availability_basis": "Latest publication date of the cited calculation inputs; not a directly disclosed amount.",
        },
    }
    entry["conflicts"][0].update(
        {
            "status": "resolved",
            "resolution_ref": calculation["calculation_ref"],
            "original_detail": entry["conflicts"][0]["detail"],
            "detail": "The original IFRS 4 opening ordinary equity is retained in superseded_facts. FY2022 selects the audited IFRS 17 derived opening balance, with all source amounts and transition adjustments retained in equity_reconciliations.",
        }
    )


def _close_costco_review(entry: dict[str, Any]) -> None:
    review = json.loads((FIXTURES / "costco-parent-equity-reviewed-facts.json").read_text())
    source = review["source_capture"]
    rel = "costco-equity/cost-20230903.htm"
    doc = _root_capture_doc(rel, "costco", "0000909832-23-000042", "sec_filing", "2023-10-11")
    entry["documents"].append(doc)
    replacements = {}
    for raw in review["reviewed_parent_equity_rows"]:
        row = {**raw, "category": "equity", "raw_path": rel}
        replacements[row["period_end"]] = _make_reviewed_fact(row, doc, published="2023-10-11")
    superseded = {}
    for year in entry["fiscal_years"]:
        for index, field in enumerate(year["facts"]):
            end = field["fact"]["period"]["end"]
            if field["category"] == "equity" and end in replacements:
                superseded[end] = field
                year["facts"][index] = replacements[end]
        beginning = year["beginning_equity_fact"]
        if beginning and beginning["fact"]["period"]["end"] in replacements:
            end = beginning["fact"]["period"]["end"]
            superseded[end] = beginning
            year["beginning_equity_fact"] = replacements[end]
    entry["superseded_facts"] = [superseded[key] for key in sorted(superseded)]
    canonical = {
        "document_id": doc["id"],
        "sha256": source["sha256"],
        "published_on": "2023-10-11",
        "url": source["url"],
    }
    periods = []
    roles = {
        "parent_equity": ("parent", "parent"),
        "noncontrolling_interest": ("nci", "nci"),
        "reported_total_equity": ("total", "total"),
    }
    reviewed_excerpts = {
        "parent": "Total Costco stockholders’ equity | 25,058 | 20,642",
        "nci": "Noncontrolling interests | — | 5",
        "total": "TOTAL EQUITY | 25,058 | 20,647",
    }
    for period in review["reported_total_and_nci"]:
        item = {"date": period["date"], "context_ref": period["context_ref"]}
        for detail in period["source_fact_details"]:
            key, scope = roles[detail["role"]]
            excerpt = _visible_text(detail["raw_html_row"])
            component = {
                "scope": scope,
                "amount": int(detail["normalized_value_usd_millions"].replace(",", "")),
                "source_ref": {
                    **canonical,
                    "context_ref": detail["context_ref"],
                    "tag": detail["tag"],
                    "source_label": detail["source_label"],
                    "locator": {"kind": "text", "anchor": excerpt},
                    "excerpt": reviewed_excerpts[key],
                },
            }
            if key == "nci":
                component["amount_notation"] = (
                    "em_dash_zero" if component["amount"] == 0 else "reported"
                )
            item[key] = component
        periods.append(item)
    entry["equity_scope_reviews"] = [
        {
            "id": "costco.parent-equity.v1",
            "currency": "USD",
            "raw_unit": "USD million",
            "scale": 1000000,
            "period_type": "instant",
            "source_ref": canonical,
            "periods": periods,
        }
    ]
    entry["conflicts"][0].update(
        {
            "status": "resolved",
            "resolution_ref": evaluate_equity_scope_review(entry["equity_scope_reviews"][0])[
                "review_ref"
            ],
            "affected_period_ends": ["2022-08-28", "2023-09-03"],
            "original_detail": entry["conflicts"][0]["detail"],
            "detail": "The earlier reviewed FY2022/FY2023 total-equity inputs including NCI remain in superseded_facts. The selected historical inputs now use directly disclosed parent stockholders' equity, consistent with later parent-equity inputs; parent, NCI and reported totals are independently retained and reconciled.",
        }
    )


def main() -> None:
    records, by_path = _capture_records()
    reviewed = _review_rows()
    _resolve_review_paths(reviewed, records)
    # Root CLP downloads were fetched and hashed separately because the issuer
    # endpoint rejected the initial collector's user agent.
    root_clp_paths = [
        p
        for p in (RAW / "root-clp-probes").glob("*.pdf")
        if p.with_suffix(p.suffix + ".meta.json").exists()
    ]
    doc_override: dict[str, dict[str, Any]] = {}
    for p in root_clp_paths:
        rel = p.relative_to(RAW).as_posix()
        doc_override[rel] = _root_clp_doc(
            rel,
            "clp-holdings",
            LATEST_PUBLISHED["clp-holdings"] if p.name == "2025-1.pdf" else None,
        )
    tencent_hkex_path = "root-tencent-publication/hkex-annual-report-2025.pdf"
    tencent_hkex_doc = _root_capture_doc(
        tencent_hkex_path,
        "tencent-holdings",
        "tencent-holdings:hkex-annual-report-2025",
        "issuer_annual",
        "2026-04-09",
    )
    tencent_hkex_index = _root_capture_doc(
        "root-tencent-publication/official-index.html",
        "tencent-holdings",
        "tencent-holdings:hkex-official-index",
        "official_index",
    )
    companies = []
    controlled = [
        "clp-holdings",
        "hsbc-holdings",
        "tencent-holdings",
        "apple",
        "costco",
        "jpmorgan-chase",
        "exxon-mobil",
        "tsmc",
    ]
    for company in controlled:
        docs, ids = _company_docs(company, records, by_path)
        for path, d in doc_override.items():
            if company == "clp-holdings" and 200 <= d["http_status"] < 300:
                docs.append(d)
                ids[path] = d
        if company == "tencent-holdings":
            docs.extend([tencent_hkex_doc, tencent_hkex_index])
            ids[tencent_hkex_path] = tencent_hkex_doc
        # Ensure the report selected by a reviewed fact is an authoritative, successful body.
        company_rows = [r for r in reviewed if r.get("company_key") == company]
        fact_docs_by_year = {}
        for row in company_rows:
            path = row["raw_path"]
            d = ids.get(path)
            if d is None and path in by_path:
                rec = by_path[path]
                d = _doc(
                    rec, doc_id=f"{company}:issuer_annual:{Path(path).name}", role="issuer_annual"
                )
                docs.append(d)
                ids[path] = d
            if d is None and path in doc_override:
                d = doc_override[path]
            if d is not None:
                fact_docs_by_year.setdefault(row["year"], d)
        # Prefer SEC filing as annual-date authority for every SEC-bound identity.
        submission_doc = None
        if company in SEC_CIK:
            selpath = RAW / "sec" / company / "selected_annual_filings.json"
            selected = json.loads(selpath.read_text(encoding="utf-8")) if selpath.exists() else []
            if selected:
                latest = max(selected, key=lambda r: (r["reportDate"], r["filingDate"]))
                end = latest["reportDate"]
                pub = latest["filingDate"]
                form = latest["form"]
                filing_path = next(
                    (
                        p
                        for p, r in by_path.items()
                        if r["company_key"] == company and r["role"] == f"filing_{end}"
                    ),
                    None,
                )
                filing_doc = ids.get(filing_path) if filing_path else None
                submission_doc = next((d for d in docs if d["role"] == "submissions_index"), None)
                companyfacts_doc = next((d for d in docs if d["role"] == "sec_companyfacts"), None)
                if filing_doc:
                    filing_doc["published_on"] = pub
                if filing_path and filing_doc:
                    fact_docs_by_year[int(end[:4])] = filing_doc
                discovery = (
                    {"document_id": submission_doc["id"], "checked_through": AS_OF}
                    if submission_doc
                    else None
                )
                period_start = next(
                    (
                        r["period_start"]
                        for r in company_rows
                        if r.get("year") == int(end[:4]) and r.get("period_end") == end
                    ),
                    None,
                )
                if period_start is None:
                    start_map = {
                        "apple": "2024-09-29",
                        "costco": "2025-09-01",
                        "jpmorgan-chase": "2025-01-01",
                        "exxon-mobil": "2025-01-01",
                        "hsbc-holdings": "2025-01-01",
                        "tsmc": "2025-01-01",
                    }
                    period_start = start_map[company]
                latest_annual = {
                    "period_start": period_start,
                    "period_end": end,
                    "published_on": pub,
                    "form": form,
                    "document_id": filing_doc["id"] if filing_doc else "",
                    "discovery": discovery,
                }
                if companyfacts_doc is None:
                    pass
            else:
                latest_annual = None
        else:
            year = max((int(r["year"]) for r in company_rows), default=0)
            row = next((r for r in company_rows if int(r["year"]) == year), None)
            report = fact_docs_by_year.get(year)
            index_doc = next((d for d in docs if d["role"] == "official_index"), None)
            latest_annual = None
            if row and report:
                pub = LATEST_PUBLISHED.get(company)
                report["published_on"] = pub
                latest_annual = {
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "published_on": pub,
                    "form": "Annual Report",
                    "document_id": report["id"],
                    "discovery": None,
                }
                if index_doc and company == "tencent-holdings":
                    hkex_visible = _visible_text(
                        (RAW / tencent_hkex_index["path"]).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )
                    anchor = "ANNUAL REPORT 2025"
                    at = hkex_visible.find(anchor)
                    hkex_excerpt = hkex_visible[max(0, at - 160) : at + len(anchor) + 30]
                    link = _index_link(tencent_hkex_index, tencent_hkex_doc)
                    publication = {
                        "document_id": tencent_hkex_index["id"],
                        "locator": {"kind": "text", "anchor": hkex_excerpt},
                        "excerpt": hkex_excerpt,
                        "published_on": "2026-04-09",
                    }
                    tencent_hkex_doc["published_on"] = "2026-04-09"
                    latest_annual.update(
                        {
                            "published_on": "2026-04-09",
                            "document_id": tencent_hkex_doc["id"],
                            "publication_evidence": publication,
                            "discovery": {
                                "document_id": tencent_hkex_index["id"],
                                "checked_through": AS_OF,
                                "locator": {"kind": "text", "anchor": hkex_excerpt},
                                "excerpt": hkex_excerpt,
                                "link_url": link,
                            },
                        }
                    )
                elif index_doc:
                    link = _index_link(index_doc, report)
                    visible = (RAW / index_doc["path"]).read_text(
                        encoding="utf-8", errors="replace"
                    )
                    title = f"{year} Annual Report"
                    index_text = _visible_text(visible)
                    at = index_text.casefold().find(title.casefold())
                    excerpt = (
                        index_text[max(0, at - 80) : at + len(title) + 160] if at >= 0 else title
                    )
                    publication = None
                    if company == "clp-holdings":
                        publication = {
                            "document_id": report["id"],
                            "locator": {"kind": "pdf", "page": 253},
                            "excerpt": "Annual Report – Publication Dates\nOnline: 12 March 2026",
                            "published_on": pub,
                        }
                    if company == "hsbc-holdings":
                        # Page 6 of the source report has its explicit publication date.
                        publication = {
                            "document_id": report["id"],
                            "locator": {"kind": "pdf", "page": 6},
                            "excerpt": "25 February 2026",
                            "published_on": pub,
                        }
                    latest_annual["discovery"] = {
                        "document_id": index_doc["id"],
                        "checked_through": AS_OF,
                        "locator": {"kind": "text", "anchor": title},
                        "excerpt": excerpt,
                        "link_url": link,
                        **({"publication_evidence": publication} if publication else {}),
                    }
                    if publication:
                        latest_annual["publication_evidence"] = publication
        latest_year = int(latest_annual["period_end"][:4]) if latest_annual else 0
        rows_by_year: dict[int, dict[str, dict[str, Any]]] = {}
        beginning_row = None
        for row in company_rows:
            year = int(row["year"])
            if row["category"] in {"equity", "beginning_equity"} and year == latest_year - 4:
                beginning_row = row
            if year >= latest_year - 3 and year <= latest_year:
                rows_by_year.setdefault(year, {})[row["category"]] = row
        fields = []
        fiscal_years = []
        for year in sorted(rows_by_year):
            rowset = rows_by_year[year]
            facts = []
            for category, row in rowset.items():
                doc = ids.get(row.get("raw_path")) or fact_docs_by_year.get(year)
                if doc is None:
                    continue
                pub = (
                    row.get("filing_date")
                    or doc.get("published_on")
                    or LATEST_PUBLISHED.get(company)
                )
                fact_row = row.copy()
                if (
                    category == "capex"
                    and company in {"hsbc-holdings", "tencent-holdings"}
                    and year == latest_year
                ):
                    reason = (
                        "Banking operations do not expose a comparable gross industrial PPE-capex/FCF measure; the cited amount nets PPE purchases and disposals."
                        if company == "hsbc-holdings"
                        else "The reported cash purchase combines PPE, construction in progress and investment property; it is not a pure PPE-capex measure."
                    )
                    fact_row["value"] = None
                    fact_row.setdefault("notes", []).append(reason)
                if company == "jpmorgan-chase" and category == "capex":
                    fact_row["source_label"] = "a financial holding company"
                    fact_row["source_field"] = "not_applicable_by_financial_sector_definition"
                    fact_row["excerpt"] = (
                        "a financial holding company incorporated under Delaware law in 1968"
                    )
                    fact_row["raw_path"] = doc["path"]
                    fact_row["locator"] = "Company overview: financial holding company"
                    fact_row["locator_detail"] = {
                        "kind": "text",
                        "anchor": "a financial holding company",
                    }
                record = _make_reviewed_fact(fact_row, doc, published=pub)
                facts.append(record)
                if year == latest_year:
                    fields.append(record)
            start = next((r["period_start"] for r in rowset.values()), f"{year}-01-01")
            end = next((r["period_end"] for r in rowset.values()), f"{year}-12-31")
            beginning = None
            prior_equity = rows_by_year.get(year - 1, {}).get("equity")
            if prior_equity is not None:
                bdoc = ids.get(prior_equity.get("raw_path")) or fact_docs_by_year.get(year - 1)
                if bdoc:
                    b = prior_equity.copy()
                    b["period_start"] = start
                    b["period_end"] = end
                    beginning = _make_reviewed_fact(
                        b,
                        bdoc,
                        published=prior_equity.get("filing_date")
                        or bdoc.get("published_on")
                        or LATEST_PUBLISHED.get(company),
                        beginning=True,
                    )
            elif year == latest_year - 3 and beginning_row is not None:
                bdoc = ids.get(beginning_row.get("raw_path")) or fact_docs_by_year.get(
                    latest_year - 4
                )
                if bdoc:
                    b = beginning_row.copy()
                    b["period_start"] = start
                    b["period_end"] = end
                    beginning = _make_reviewed_fact(
                        b,
                        bdoc,
                        published=beginning_row.get("filing_date")
                        or bdoc.get("published_on")
                        or LATEST_PUBLISHED.get(company),
                        beginning=True,
                    )
            fy = {
                "label": f"FY{year}",
                "period_start": start,
                "period_end": end,
                "facts": facts,
                "beginning_equity_fact": (
                    {
                        "category": "equity",
                        "fact": beginning["fact"],
                        "evidence": beginning["evidence"],
                    }
                    if beginning
                    else None
                ),
            }
            fiscal_years.append(fy)
        # SEC latest inline-review rows are added as their own facts and evidence;
        # when a company has no reviewed row yet, leave fields empty and the
        # validator explicitly reports that gap.
        if company in SEC_CIK and not fields:
            fields = []
        expected = []
        if company in SEC_CIK:
            subbody = json.loads(
                (RAW / "sec" / company / "submissions.json").read_text(encoding="utf-8")
            )
            by_filename = {}
            for d in docs:
                if d["role"] != "sec_shard":
                    continue
                match = re.search(r"CIK\d+-submissions-\d+\.json", d["url"])
                if match:
                    by_filename[match.group(0)] = d
            for shard in subbody.get("filings", {}).get("files", []):
                name = shard["name"]
                d = by_filename.get(name)
                if d:
                    expected.append({"path": d["path"], "document_id": d["id"]})
        conflict = []
        if company == "hsbc-holdings":
            conflict = [
                {
                    "kind": "ifrs_17_restatement",
                    "period": "FY2021/FY2022 comparative and opening equity",
                    "detail": "HSBC FY2021 was originally reported under IFRS 4 and later presented on an IFRS 17-restated basis; the evidence uses the FY2025 report's consistent restated FY2022–FY2025 series and does not substitute contemporaneous IFRS 4 comparatives.",
                }
            ]
        if company == "costco":
            conflict = [
                {
                    "kind": "equity_scope_tag_change",
                    "period": "FY2022–FY2023 vs FY2024–FY2026",
                    "detail": "Reviewed SEC history uses Costco total equity including NCI for FY2022–FY2023 per the reported tag, while later filing rows use StockholdersEquity. Scope is retained as reported; do not treat the four-year denominator series as fully scope-homogeneous.",
                }
            ]
        entry = {
            "company_key": company,
            "report_currency": CURRENCY[company],
            "latest_annual": latest_annual,
            "documents": docs,
            "fields": fields,
            "fiscal_years": fiscal_years,
            "expected_shards": expected,
            "conflicts": conflict,
        }
        _close_equity_reviews(entry)
        companies.append(entry)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {"schema_version": 1, "as_of": AS_OF, "companies": companies},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {MANIFEST}: {len(companies)} company entries; reviewed fact count={sum(len(c['fields']) for c in companies)}"
    )


def _visible_text(raw: str) -> str:
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_starttag(self, tag, attrs):
            if tag.casefold() in {
                "br",
                "p",
                "div",
                "tr",
                "td",
                "th",
                "li",
                "section",
                "h1",
                "h2",
                "h3",
            }:
                self.parts.append(" ")

        def handle_endtag(self, tag):
            if tag.casefold() in {"p", "div", "tr", "li", "section", "h1", "h2", "h3"}:
                self.parts.append(" ")

        def handle_data(self, data):
            self.parts.append(data)

    parser = Parser()
    parser.feed(raw)
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _visible_for_doc(path: str) -> str:
    if path not in _VISIBLE_CACHE:
        _VISIBLE_CACHE[path] = _visible_text(
            (RAW / path).read_text(encoding="utf-8", errors="replace")
        )
    return _VISIBLE_CACHE[path]


if __name__ == "__main__":
    main()
