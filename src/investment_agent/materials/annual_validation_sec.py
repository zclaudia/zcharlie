"""SEC annual-filing window checks for the annual evidence package."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from investment_agent.materials.annual_validation import _date

_SEC_HOSTS = frozenset({"sec.gov"})
_SEC_ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "20-F", "20-F/A"})

def _sec_annual_filings(
    documents: Mapping[str, Mapping[str, Any]],
) -> tuple[list[tuple[date, date, str]], list[str]]:
    rows: list[tuple[date, date, str]] = []
    problems: list[str] = []
    for doc_id, document in documents.items():
        if not isinstance(document.get("role"), str) or document.get("role") not in {
            "submissions_index",
            "sec_shard",
            "submissions_shard",
        }:
            continue
        body = document.get("_body")
        if not isinstance(body, bytes):
            problems.append(f"SEC filing window document {doc_id} has no raw body")
            continue
        try:
            payload = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            problems.append(f"SEC filing window document {doc_id} is not valid JSON")
            continue
        if not isinstance(payload, Mapping):
            problems.append(f"SEC filing window document {doc_id} must be a JSON object")
            continue
        filings = payload.get("filings")
        if document.get("role") == "submissions_index":
            table = filings.get("recent") if isinstance(filings, Mapping) else None
        else:
            # SEC historical submissions JSON stores the filing columns at the
            # top level (accessionNumber/form/reportDate/filingDate).
            table = (
                payload
                if "accessionNumber" in payload
                else (
                    filings
                    if isinstance(filings, Mapping) and "accessionNumber" in filings
                    else filings.get("recent")
                    if isinstance(filings, Mapping)
                    else None
                )
            )
        if not isinstance(table, Mapping):
            problems.append(f"SEC filing window document {doc_id} has no filing table")
            continue
        forms = table.get("form")
        reports = table.get("reportDate")
        filed = table.get("filingDate")
        if (
            not isinstance(forms, list)
            or not isinstance(reports, list)
            or not isinstance(filed, list)
        ):
            problems.append(
                f"SEC filing window document {doc_id} lacks form/reportDate/filingDate arrays"
            )
            continue
        if not (len(forms) == len(reports) == len(filed)):
            problems.append(
                f"SEC filing window document {doc_id} has mismatched filing array lengths"
            )
            continue
        for form, report_date, filing_date in zip(forms, reports, filed, strict=True):
            if form not in _SEC_ANNUAL_FORMS:
                continue
            period_end, published = _date(report_date), _date(filing_date)
            if period_end is None or published is None:
                problems.append(f"SEC annual filing in {doc_id} has invalid reportDate/filingDate")
                continue
            rows.append((period_end, published, form))
    return rows, problems


def _check_sec_latest_annual(
    annual: Mapping[str, Any] | None,
    documents: Mapping[str, Mapping[str, Any]],
    as_of: date,
    issues: list[str],
) -> bool:
    issue_count = len(issues)
    rows, problems = _sec_annual_filings(documents)
    issues.extend(problems)
    eligible = [row for row in rows if row[0] <= as_of and row[1] <= as_of]
    if not eligible:
        issues.append("saved SEC submissions/shards contain no annual filing available by as_of")
        return False
    latest = max(eligible, key=lambda row: (row[0], row[1]))
    if annual is None:
        issues.append("latest_annual is missing the SEC selected filing comparison")
        return False
    if _date(annual.get("period_end")) != latest[0]:
        issues.append("latest_annual period_end disagrees with latest SEC reportDate")
    if _date(annual.get("published_on")) != latest[1]:
        issues.append("latest_annual published_on disagrees with latest SEC filingDate")
    if annual.get("form") != latest[2]:
        issues.append("latest_annual form disagrees with the latest SEC annual form")
    return len(issues) == issue_count
