#!/usr/bin/env python3
"""Fetch primary issuer / SEC annual evidence for the HK2-04 pilot.

The network response and a JSON sidecar are written under the ignored data tree;
no financial facts are inferred by this collector. Facts are reviewed separately
and only then included in the versioned validation manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
CIKS = {
    "apple": "0000320193",
    "costco": "0000909832",
    "jpmorgan-chase": "0000019617",
    "exxon-mobil": "0000034088",
    "hsbc-holdings": "0001089113",
    "tsmc": "0001046179",
}
HK_URLS = {
    "clp-holdings": {
        2022: "https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/3-3-financial-reports/2022/e_2022%20Annual%20Report.pdf.coredownload.pdf",
        2023: "https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/3-3-financial-reports/2023/e_2023%20Annual%20Report.pdf.coredownload.pdf",
        2024: "https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/3-3-financial-reports/2024/e_2024%20Annual%20Report.pdf.coredownload.pdf",
        2025: "https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/3-3-financial-reports/2025/e_2025%20Annual%20Report.pdf.coredownload.pdf",
    },
    "hsbc-holdings": {
        2022: "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2022/annual/pdfs/hsbc-holdings-plc/230221-annual-report-and-accounts-2022.pdf",
        2023: "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/pdfs/hsbc-holdings-plc/240221-annual-report-and-accounts-2023.pdf",
        2024: "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/annual/pdfs/hsbc-holdings-plc/250219-annual-report-and-accounts-2024.pdf",
        2025: "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2025/annual/pdfs/hsbc-holdings-plc/260225-annual-report-and-accounts-2025.pdf",
    },
    "tencent-holdings": {
        2022: "https://static.www.tencent.com/uploads/2023/04/06/214dce4c5312264800b20cfab64861ba.pdf",
        2023: "https://static.www.tencent.com/uploads/2024/04/08/e95c902973fc282be3b3e285c6245281.pdf",
        2024: "https://static.www.tencent.com/uploads/2025/04/08/0706a9085e70140122364ded872455ca.pdf",
        2025: "https://static.www.tencent.com/uploads/2026/04/09/62d786fcf3d3c8cb7e54791ee95439ac.pdf",
    },
}
OFFICIAL_INDEXES = {
    "clp-holdings": "https://www.clpgroup.com/en/investor-relations/financial-reporting/financial-reports.html",
    "hsbc-holdings": "https://www.hsbc.com/investors/results-and-announcements/annual-report",
    "tencent-holdings": "https://www.tencent.com/en-us/investors/financial-reports.html",
}
USER_AGENT = "zcharlie annual evidence validation (personal research)"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class Collector:
    def __init__(self, output: Path, timeout: int = 60) -> None:
        self.output = output
        self.timeout = timeout
        self.session = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
            timeout=timeout,
        )
        self.records: list[dict[str, Any]] = []

    def fetch(
        self,
        company: str,
        role: str,
        url: str,
        *,
        rel: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        relpath = rel or f"{company}/{_safe(role)}{Path(url.split('?')[0]).suffix or '.bin'}"
        path = self.output / relpath
        if path.exists() or path.with_suffix(path.suffix + ".meta.json").exists():
            attempt = 2
            while path.with_name(f"{path.stem}.attempt-{attempt}{path.suffix}").exists():
                attempt += 1
            path = path.with_name(f"{path.stem}.attempt-{attempt}{path.suffix}")
            relpath = str(path.relative_to(self.output))
        path.parent.mkdir(parents=True, exist_ok=True)
        requested_at = _now()
        status: int | None = None
        response_headers: dict[str, str] = {}
        error: str | None = None
        body = b""
        try:
            response = self.session.get(url, headers=headers)
            status = response.status_code
            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower() in {"content-type", "content-length", "last-modified", "etag"}
            }
            body = response.content
            response.raise_for_status()
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            if getattr(exc, "response", None) is not None:
                body = exc.response.content
                status = exc.response.status_code
                response_headers = {
                    k: v
                    for k, v in exc.response.headers.items()
                    if k.lower() in {"content-type", "content-length", "last-modified", "etag"}
                }
        path.write_bytes(body)
        rec = {
            "company_key": company,
            "role": role,
            "document_role": self._document_role(role),
            "url": url,
            "path": relpath,
            "requested_at": requested_at,
            "captured_at": _now(),
            "http_status": status,
            "ok": error is None and status is not None and 200 <= status < 300,
            "error": error,
            "media_type": next(
                (
                    value
                    for key, value in response_headers.items()
                    if key.casefold() == "content-type"
                ),
                None,
            ),
            "response_headers": response_headers,
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        path.with_suffix(path.suffix + ".meta.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.records.append(rec)
        print(f"{rec['http_status']} {rec['bytes']:>10} {rec['sha256'][:12]} {company}/{role}")
        return rec

    @staticmethod
    def _document_role(role: str) -> str:
        if role.startswith("annual_report_"):
            return "issuer_annual"
        if role.startswith("filing_") and not role.startswith("filing_index_"):
            return "sec_filing"
        if role.startswith("filing_index_"):
            return "sec_filing_index"
        if role == "submissions":
            return "submissions_index"
        if role.startswith("submission_shard_"):
            return "sec_shard"
        if role == "companyfacts":
            return "sec_companyfacts"
        if role == "official_index":
            return "official_index"
        return role

    def sec_company(self, company: str, cik: str, as_of: str) -> None:
        facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        facts_rec = self.fetch(
            company, "companyfacts", facts_url, rel=f"sec/{company}/companyfacts.json"
        )
        sub_rec = self.fetch(company, "submissions", sub_url, rel=f"sec/{company}/submissions.json")
        if not facts_rec["ok"] or not sub_rec["ok"]:
            return
        data = json.loads((self.output / sub_rec["path"]).read_text(encoding="utf-8"))
        recent = data.get("filings", {}).get("recent", {})
        rows = []
        for i, form in enumerate(recent.get("form", [])):
            if form not in {"10-K", "20-F"}:
                continue
            if recent["filingDate"][i] > as_of:
                continue
            rows.append(
                {
                    key: recent.get(key, [None] * len(recent["form"]))[i]
                    for key in (
                        "form",
                        "filingDate",
                        "reportDate",
                        "accessionNumber",
                        "primaryDocument",
                    )
                }
            )
        # Preserve every shard named by the submissions index. This makes the
        # index response replayable and exposes the exact historical coverage.
        for shard in data.get("filings", {}).get("files", []):
            shard_name = shard["name"]
            shard_url = f"https://data.sec.gov/submissions/{quote(shard_name)}"
            shard_rec = self.fetch(
                company,
                f"submission_shard_{shard_name}",
                shard_url,
                rel=f"sec/{company}/{shard_name}",
            )
            if not shard_rec["ok"]:
                continue
            shard_data = json.loads((self.output / shard_rec["path"]).read_text(encoding="utf-8"))
            for i, form in enumerate(shard_data.get("form", [])):
                if form not in {"10-K", "20-F"} or shard_data["filingDate"][i] > as_of:
                    continue
                rows.append(
                    {
                        key: shard_data.get(key, [None] * len(shard_data["form"]))[i]
                        for key in (
                            "form",
                            "filingDate",
                            "reportDate",
                            "accessionNumber",
                            "primaryDocument",
                        )
                    }
                )
        rows = sorted(
            {row["accessionNumber"]: row for row in rows if row.get("accessionNumber")}.values(),
            key=lambda x: (x.get("reportDate") or "", x.get("filingDate") or ""),
            reverse=True,
        )[:4]
        (self.output / "sec" / company / "selected_annual_filings.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for row in rows:
            accession = row["accessionNumber"].replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
            self.fetch(
                company,
                f"filing_{row['reportDate']}",
                f"{base}/{quote(row['primaryDocument'])}",
                rel=f"sec/{company}/{row['reportDate']}/{_safe(row['primaryDocument'])}",
            )
            self.fetch(
                company,
                f"filing_index_{row['reportDate']}",
                f"{base}/index.json",
                rel=f"sec/{company}/{row['reportDate']}/index.json",
            )

    def run(self, as_of: str) -> None:
        for company, urls in HK_URLS.items():
            for year, url in urls.items():
                self.fetch(
                    company,
                    f"annual_report_{year}",
                    url,
                    rel=f"issuer/{company}/{year}/annual_report{Path(url.split('?')[0]).suffix or '.pdf'}",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
                    },
                )
        for company, url in OFFICIAL_INDEXES.items():
            self.fetch(
                company,
                "official_index",
                url,
                rel=f"issuer/{company}/official-index.html",
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
                },
            )
        for company, cik in CIKS.items():
            self.sec_company(company, cik, as_of)
        (self.output / "capture-index.json").write_text(
            json.dumps(
                {"as_of": as_of, "user_agent": USER_AGENT, "documents": self.records},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Captured {len(self.records)} responses under {self.output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of", default="2026-10-15", help="Client validation date (not response capture time)."
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "data/hk2-latest-evidence/2026-10-15/raw"
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    Collector(args.output, args.timeout).run(args.as_of)
    return 0


if __name__ == "__main__":
    sys.exit(main())
