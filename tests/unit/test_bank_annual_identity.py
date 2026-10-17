from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from investment_agent.industry_peers.bank_annual_identity import (
    annual_front_matter,
    checked_bank_front_identity,
)
from tests.support.bank_annual_v2 import bank_annual_pdf


@pytest.mark.parametrize(
    "heading",
    ["江苏银行 年度报告2024ANNUAL REPORT", "中国工商银行 2024年年度报告", "工商银行2024/年度报告"],
)
def test_actual_cover_heading_variants(heading):
    assert annual_front_matter(bank_annual_pdf(2024, "10.00", "9.00", cover_title=heading), 2024)


def test_titleless_cover_may_put_annual_heading_on_second_page():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.append_pages_from_reader(PdfReader(BytesIO(bank_annual_pdf(2024, "10.00", "9.00"))))
    out = BytesIO()
    writer.write(out)
    assert "2024" in annual_front_matter(out.getvalue(), 2024)


@pytest.mark.parametrize("heading", ["工商银行2025年度报告", "工商银行2024年度报告摘要"])
def test_wrong_year_and_summary_cannot_use_comparative_tables(heading):
    with pytest.raises(ValueError):
        annual_front_matter(bank_annual_pdf(2024, "10.00", "9.00", cover_title=heading), 2024)


def test_legal_issuer_name_supports_common_bank_name_without_inventing_ticker():
    body = bank_annual_pdf(
        2024, "10.00", "9.00", cover_title="江苏常熟农村商业银行股份有限公司2024年年度报告"
    )
    assert checked_bank_front_identity(body, 2024, "601128", "常熟银行")
    with pytest.raises(ValueError, match="issuer"):
        checked_bank_front_identity(body, 2024, "601009", "南京银行")
