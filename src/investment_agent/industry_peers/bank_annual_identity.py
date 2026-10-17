"""Annual identity on actual front matter, including titleless cover pages."""

from __future__ import annotations

import re
from functools import lru_cache
from io import BytesIO

from investment_agent.industry_peers.bank_pdf_text import bank_page_text

_YEAR = r"(?<!\d)(20\d{2})(?!\d)"
_ANNUAL = r"(?:年\s*)?年\s*度\s*报\s*告"
_HEADINGS = (
    re.compile(_YEAR + r"\s*(?:/\s*)?" + _ANNUAL),
    re.compile(_ANNUAL + r"\s*" + _YEAR),
    re.compile(_YEAR + r"\s*ANNUAL\s*REPORT", re.I),
    re.compile(r"ANNUAL\s*REPORT\s*" + _YEAR, re.I),
)

BANK_LEGAL_NAMES = {
    "601398": "中国工商银行股份有限公司",
    "601939": "中国建设银行股份有限公司",
    "601288": "中国农业银行股份有限公司",
    "600036": "招商银行股份有限公司",
    "601166": "兴业银行股份有限公司",
    "000001": "平安银行股份有限公司",
    "600919": "江苏银行股份有限公司",
    "601009": "南京银行股份有限公司",
    "002142": "宁波银行股份有限公司",
    "601128": "江苏常熟农村商业银行股份有限公司",
}


@lru_cache(maxsize=24)
def annual_front_matter(body: bytes, year: int) -> str:
    """Find the first actual annual heading, never a later comparative column."""
    from pypdf import PdfReader

    count = min(3, len(PdfReader(BytesIO(body)).pages))
    pages = []
    for number in range(1, count + 1):
        page = bank_page_text(body, number)
        pages.append(page)
        matches = [match for rule in _HEADINGS for match in rule.finditer(page)]
        if not matches:
            continue
        if any(int(match.group(1)) != year for match in matches):
            raise ValueError("the PDF cover year differs from the target annual period")
        for match in matches:
            # Other summary words in ordinary contents are not report titles.
            suffix = re.sub(r"\s+", "", page[match.end() : match.end() + 16])
            if suffix.startswith(("摘要", "SUMMARY")):
                raise ValueError("the PDF annual heading identifies a summary")
        return "\n".join(pages)
    raise ValueError("the PDF front matter has no exact target-year annual heading")


@lru_cache(maxsize=24)
def checked_bank_front_identity(body: bytes, year: int, code: str, company_name: str) -> str:
    """An exact annual heading and early issuer page may be separate pages."""
    from pypdf import PdfReader

    front = annual_front_matter(body, year)
    aliases = [name for name in (code, company_name, BANK_LEGAL_NAMES.get(code)) if name]

    def identified(text: str) -> bool:
        compact = re.sub(r"\s+", "", text)
        return any(name in compact for name in aliases)

    if identified(front):
        return front
    pages = [front]
    count = min(8, len(PdfReader(BytesIO(body)).pages))
    for number in range(1, count + 1):
        text = bank_page_text(body, number)
        pages.append(text)
        if identified(text):
            return "\n".join(pages)
    raise ValueError("annual-report front matter does not identify the selected issuer")
