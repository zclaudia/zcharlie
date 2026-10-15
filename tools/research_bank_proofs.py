#!/usr/bin/env python3
"""Acquire official FY2025 bank filings and build field audit evidence.

This is a bounded research helper. It does not feed the production collector.
All HTTP requests use the Requests default TLS verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
RUN02 = ROOT / "research/industry-data-validation/evidence/run-02"
OUT = ROOT / "research/industry-data-validation/evidence/bank-proof-2026-10-15"
FIXTURE = ROOT / "tests/fixtures/bank_proofs_v2"
SAMPLE = [
    ("601398.SH", "工商银行"), ("601939.SH", "建设银行"),
    ("601288.SH", "农业银行"), ("600036.SH", "招商银行"),
    ("601166.SH", "兴业银行"), ("000001.SZ", "平安银行"),
    ("600919.SH", "江苏银行"), ("601009.SH", "南京银行"),
    ("002142.SZ", "宁波银行"), ("601128.SH", "常熟银行"),
]
METRICS = {
    "equity_parent": ("归属于母公司股东权益合计", "CNY", "point_in_time"),
    "roe_weighted_annual": ("加权平均净资产收益率", "fraction", "annual"),
    "net_interest_margin": ("净息差", "fraction", "annual"),
    "npl_ratio": ("不良贷款率", "fraction", "point_in_time"),
    "provision_coverage": ("拨备覆盖率", "fraction", "point_in_time"),
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_stock_list() -> dict[str, dict[str, str]]:
    url = "https://www.cninfo.com.cn/new/data/szse_stock.json"
    response = requests.get(url, timeout=40)
    response.raise_for_status()
    payload = response.json()
    items = payload.get("stockList")
    if not isinstance(items, list):
        raise ValueError("CNINFO stock list lacks stockList")
    return {item["code"]: item for item in items if item.get("code")}


def query_filing(code: str, org: str) -> dict[str, Any]:
    endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    candidates: list[dict[str, Any]] = []
    attempts = []
    for keyword in ("2025", "2025年年度报告", "2025年度报告"):
        form = {
            "pageNum": "1", "pageSize": "30",
            "column": "sse" if code.endswith("SH") else "szse",
            "tabName": "fulltext", "stock": f"{code[:6]},{org}",
            "searchkey": keyword, "category": "category_ndbg_szsh",
            "seDate": "2026-01-01~2026-10-15", "isHLtitle": "false",
            "sortName": "time", "sortType": "desc",
        }
        response = requests.post(endpoint, data=form, timeout=40)
        response.raise_for_status()
        payload = response.json()
        if payload.get("hasMore"):
            raise ValueError(f"CNINFO query has unexamined pages for {code}")
        body = response.content
        attempts.append({"keyword": keyword, "request": form, "response": payload,
                         "response_sha256": sha(body), "status": response.status_code,
                         "fetched_at": datetime.now(UTC).isoformat()})
        for item in payload.get("announcements") or []:
            title = re.sub(r"<[^>]+>", "", item.get("announcementTitle", ""))
            if (item.get("secCode") == code[:6]
                    and re.search(r"2025年?年度报告", title)
                    and not any(x in title for x in ("摘要", "英文", "取消", "提示", "审计", "问询", "回复"))):
                candidates.append(item)
        if candidates:
            break
    if not candidates:
        raise ValueError(f"No official CNINFO FY2025 full annual report found for {code}")
    chosen = max(candidates, key=lambda x: x["announcementTime"])
    return {"code": code, "candidates": candidates, "chosen": chosen, "query_attempts": attempts}


def acquire() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stock_map = get_stock_list()
    source_map = {}
    for code, name in SAMPLE:
        old_pdf = RUN02 / "raw/official" / f"{code}_2025.pdf"
        old_meta = RUN02 / "derived/filings" / f"{code}.json"
        if old_pdf.exists() and old_meta.exists():
            filing = json.loads(old_meta.read_text(encoding="utf-8"))
            source_map[code] = {"source": "run-02", "pdf_path": str(old_pdf.relative_to(ROOT)),
                                "metadata": filing, "acquired_at": None}
            continue
        stock = stock_map[code[:6]]
        filing = query_filing(code, stock["orgId"])
        chosen = filing["chosen"]
        url = "https://static.cninfo.com.cn/" + chosen["adjunctUrl"]
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        if not response.content.startswith(b"%PDF-"):
            raise ValueError(f"CNINFO response is not a PDF for {code}")
        acquired_at = datetime.now(UTC).isoformat()
        pdf_path = OUT / "originals" / f"{code}_2025.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(response.content)
        metadata = {
            **filing, "name": name, "url": url,
            "pdf_sha256": sha(response.content), "pdf_bytes": len(response.content),
            "pdf_pages": len(PdfReader(pdf_path).pages),
            "acquired_at": acquired_at,
            "tls_verification": "requests_default_enabled",
            "download_status": response.status_code,
            "download_final_url": response.url,
            "download_headers": {k: v for k, v in response.headers.items()
                                 if k.lower() in {"content-type", "content-length", "last-modified", "etag"}},
        }
        write_json(OUT / "filings" / f"{code}.json", metadata)
        source_map[code] = {"source": "downloaded", "pdf_path": str(pdf_path.relative_to(ROOT)),
                            "metadata_path": str((OUT / "filings" / f"{code}.json").relative_to(ROOT)),
                            "metadata": metadata, "acquired_at": acquired_at}
        print(f"acquired {code}: {metadata['pdf_pages']} pages {metadata['pdf_sha256']}", flush=True)
    write_json(OUT / "source-manifest.json", {
        "schema": "bank-proof-source-manifest/1", "report_period": "2025-12-31",
        "created_at": datetime.now(UTC).isoformat(), "sources": source_map,
    })


if __name__ == "__main__":
    acquire()
