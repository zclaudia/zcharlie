#!/usr/bin/env python3
"""Rebuild HSBC's audited IFRS 4 to IFRS 17 opening ordinary-equity bridge.

This reviewer is offline: it reads the captured issuer report and publication
index, validates their capture sidecars, extracts the cited report pages, and
recreates the frozen review record. It never retrieves source material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/hk2-latest-evidence/2026-10-15/raw/hsbc-restatement"
OUTPUT = ROOT / "tests/fixtures/materials/annual_validation/hsbc-restatement-reviewed-facts.json"
REPORT_PATH = "hsbc-annual-report-2023.pdf"
INDEX_PATH = "official-report-index-2023.html"
REPORT_URL = (
    "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/pdfs/"
    "hsbc-holdings-plc/240226-annual-report-and-accounts-2023.pdf?download=1"
)
INDEX_URL = (
    "https://www.hsbc.com/investors/results-and-announcements/all-reporting/group"
    "?page=1&reporting-type=annual&take=20&year=2024"
)


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _compact(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").replace("‘", "’").replace("ʼ", "’").split())


def _source(path: str, expected_url: str) -> dict[str, Any]:
    body = RAW / path
    sidecar = body.with_suffix(body.suffix + ".meta.json")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    content = body.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if metadata.get("url") != expected_url:
        raise ValueError(f"unexpected source URL in {sidecar}")
    if metadata.get("http_status") != 200 or metadata.get("ok") is not True:
        raise ValueError(f"source capture was not successful: {sidecar}")
    if metadata.get("bytes") != len(content) or metadata.get("sha256") != digest:
        raise ValueError(f"source capture hash/byte count mismatch: {body}")
    return {
        "raw_path": f"hsbc-restatement/{path}",
        "url": metadata["url"],
        "final_url": metadata.get("final_url", metadata["url"]),
        "captured_at": metadata["captured_at"],
        "http_status": metadata["http_status"],
        "media_type": metadata["media_type"],
        "bytes": len(content),
        "sha256": digest,
    }


def _extract_pages(report: Path) -> tuple[str, str]:
    reader = PdfReader(report)
    if len(reader.pages) != 450:
        raise ValueError(
            f"expected 450-page Annual Report and Accounts 2023, got {len(reader.pages)}"
        )
    transition = _compact(reader.pages[423].extract_text() or "")
    balance_sheet = _compact(reader.pages[426].extract_text() or "")
    transition_row = (
        "Total shareholders’ equity 198,250 92,738 4,558 (99,631) (8,847) 1,947 189,015 (9,235)"
    )
    if transition_row not in transition:
        raise ValueError("audited IFRS 17 transition table row did not match PDF page 424")
    if (
        "IFRS 17 transition impact on the Group consolidated balance sheet at 1 January 2022"
        not in transition
    ):
        raise ValueError("transition table heading did not match PDF page 424")
    if "Other equity instruments 19,746 22,414 19,746 22,414" not in balance_sheet:
        raise ValueError("other equity instrument values did not match PDF page 427")
    if "Total shareholders’ equity 177,833 189,015 187,484 198,250" not in balance_sheet:
        raise ValueError("restated/original shareholder-equity balances did not match PDF page 427")
    if "At 1 Jan 2022" not in balance_sheet and "1 Jan 2022" not in balance_sheet:
        raise ValueError("transition-date column missing from PDF page 427")
    return transition, balance_sheet


def _publication_excerpt(index_body: Path) -> str:
    parser = _VisibleText()
    parser.feed(index_body.read_text(encoding="utf-8", errors="replace"))
    visible = _compact(" ".join(parser.parts))
    match = re.search(
        r"21 Feb 2024 Annual Annual Results 2023 documents.{0,180}?"
        r"Annual Report and Accounts 2023 PDF 7MB 450 pages",
        visible,
    )
    if not match:
        raise ValueError(
            "official HSBC reporting index did not show the 21 Feb 2024 report listing"
        )
    return match.group(0)


def build_review() -> dict[str, Any]:
    report = _source(REPORT_PATH, REPORT_URL)
    index = _source(INDEX_PATH, INDEX_URL)
    transition_text, balance_sheet_text = _extract_pages(RAW / REPORT_PATH)
    publication_excerpt = _publication_excerpt(RAW / INDEX_PATH)

    ifrs4_total = 198250
    ifrs17_total = 189015
    other_equity_instruments = 22414
    ifrs4_ordinary = ifrs4_total - other_equity_instruments
    ifrs17_ordinary = ifrs17_total - other_equity_instruments
    transition_components = [
        {"label": "Removal of PVIF and IFRS 4 balances", "value": 92738},
        {"label": "Remeasurement effect of IFRS 9 re-designations", "value": 4558},
        {"label": "Recognition of IFRS 17 fulfilment cash flows", "value": -99631},
        {"label": "Recognition of IFRS 17 contractual service margin", "value": -8847},
        {"label": "Tax effect", "value": 1947},
    ]
    equity_adjustment = sum(item["value"] for item in transition_components)
    if ifrs4_ordinary != 175836 or ifrs17_ordinary != 166601 or equity_adjustment != -9235:
        raise AssertionError("HSBC opening ordinary-equity bridge failed its arithmetic checks")
    if ifrs17_total - ifrs4_total != equity_adjustment:
        raise AssertionError("shareholders’ equity movement does not equal transition components")

    return {
        "schema_version": 1,
        "company_key": "hsbc-holdings",
        "restatement_id": "ifrs17-opening-equity-2022-01-01",
        "transition_date": "2022-01-01",
        "reporting_currency": "USD",
        "raw_unit": "USD million",
        "scale": 1000000,
        "metric": "equity_ordinary",
        "equity_scope": "ordinary_shareholders",
        "published_on": "2024-02-21",
        "publication_evidence": {
            "document": index,
            "locator": {"kind": "text", "anchor": publication_excerpt},
            "excerpt": publication_excerpt,
        },
        "source_document": {
            **report,
            "published_on": "2024-02-21",
            "report_title": "HSBC Holdings plc Annual Report and Accounts 2023",
        },
        "original_ifrs4": {
            "value": str(ifrs4_ordinary),
            "total_shareholders_equity": str(ifrs4_total),
            "other_equity_instruments": str(other_equity_instruments),
            "period_end": "2021-12-31",
            "source_label": "Total ordinary shareholders’ equity (IFRS 4, at 31 December 2021)",
            "locator": {
                "kind": "pdf",
                "page": 427,
                "printed_page": 425,
                "row": "Total shareholders’ equity; Other equity instruments",
                "column": "Under IFRS 4, 1 Jan 2022",
            },
            "excerpt": (
                "Other equity instruments 19,746 22,414 19,746 22,414; "
                "Total shareholders’ equity 177,833 189,015 187,484 198,250"
            ),
            "reported_comparator": {
                "source_label": "Total ordinary shareholders’ equity",
                "locator": {"kind": "pdf", "page": 135, "printed_page": 133},
                "excerpt": (
                    "Total shareholders’ equity 185,329 177,833 198,250; "
                    "Preference shares and other equity instruments (17,719) (19,746) (22,414); "
                    "Total ordinary shareholders’ equity 167,610 158,087 175,836"
                ),
                "note": "2021 comparative data are prepared on an IFRS 4 basis.",
            },
        },
        "restated_ifrs17": {
            "year": 2022,
            "category": "beginning_equity",
            "metric": "equity_ordinary",
            "value": str(ifrs17_ordinary),
            "scale": 1000000,
            "raw_unit": "USD million",
            "currency": "USD",
            "period_start": None,
            "period_end": "2021-12-31",
            "source_label": "Total ordinary shareholders’ equity at transition to IFRS 17",
            "source_field": "Total shareholders’ equity less other equity instruments",
            "locator": {
                "kind": "pdf",
                "page": 427,
                "printed_page": 425,
                "row": "Total shareholders’ equity; Other equity instruments",
                "column": "Under IFRS 17, 1 Jan 2022",
            },
            "excerpt": (
                "Other equity instruments 19,746 22,414 19,746 22,414; "
                "Total shareholders’ equity 177,833 189,015 187,484 198,250"
            ),
            "raw_path": report["raw_path"],
            "notes": [
                "Derived exactly as $189,015m total shareholders’ equity less $22,414m other equity instruments; both inputs are shown in the same 1 January 2022 balance-sheet table under IFRS 17.",
                "This is the IFRS 17 restated opening ordinary-equity balance for FY2022; it does not rewrite the contemporaneous FY2021 IFRS 4 closing fact.",
            ],
        },
        "transition_bridge": {
            "basis": "Total shareholders’ equity under IFRS 17 minus unchanged other equity instruments equals ordinary shareholders’ equity.",
            "ifrs4_total_shareholders_equity": str(ifrs4_total),
            "ifrs17_total_shareholders_equity": str(ifrs17_total),
            "other_equity_instruments_under_ifrs4": str(other_equity_instruments),
            "other_equity_instruments_under_ifrs17": str(other_equity_instruments),
            "ifrs4_ordinary_equity": str(ifrs4_ordinary),
            "ifrs17_ordinary_equity": str(ifrs17_ordinary),
            "ordinary_equity_transition_adjustment": str(ifrs17_ordinary - ifrs4_ordinary),
            "total_shareholders_equity_adjustment_components": transition_components,
            "total_shareholders_equity_transition_adjustment": str(equity_adjustment),
            "transition_table_locator": {
                "kind": "pdf",
                "page": 424,
                "printed_page": 422,
                "row": "Total shareholders’ equity",
                "columns": [
                    "Under IFRS 4",
                    "Removal of PVIF and IFRS 4 balances",
                    "Remeasurement effect of IFRS 9 re-designations",
                    "Recognition of IFRS 17 fulfilment cash flows",
                    "Recognition of IFRS 17 contractual service margin",
                    "Tax effect",
                    "Under IFRS 17",
                    "Total movements",
                ],
            },
            "transition_table_excerpt": (
                "Total shareholders’ equity 198,250 92,738 4,558 (99,631) "
                "(8,847) 1,947 189,015 (9,235)"
            ),
            "transition_source_text": transition_text[
                transition_text.index(
                    "IFRS 17 transition impact on the Group consolidated balance sheet at 1 January 2022"
                ) : transition_text.index("Transition drivers")
            ],
            "opening_balance_sheet_source_text": balance_sheet_text[
                balance_sheet_text.index("Other equity instruments") : balance_sheet_text.index(
                    "Total liabilities and equity"
                )
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="write the frozen reviewed JSON fixture"
    )
    args = parser.parse_args()
    review = build_review()
    rendered = json.dumps(review, ensure_ascii=False, indent=2) + "\n"
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUTPUT}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
