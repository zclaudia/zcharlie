"""Read only the declared local CMB bytes; no download or runtime fact import."""

import json
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from investment_agent.industry_peers.bank_pdf_text import bank_page_text

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/bank_topics"
MANIFEST = json.loads((FIXTURE / "manifest.json").read_text())
EXPECTED = json.loads((FIXTURE / "expected.json").read_text())


def canonical(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def report_bytes() -> bytes:
    path = (ROOT / str(MANIFEST["source"]["path"])).resolve()
    assert path.is_relative_to(ROOT / "research/industry-data-validation/evidence/run-02")
    return path.read_bytes()


def test_original_bytes_match_all_existing_local_metadata(report_bytes: bytes) -> None:
    source = MANIFEST["source"]
    assert len(report_bytes) == source["bytes"] == 32_081_947
    assert sha256(report_bytes).hexdigest() == source["sha256"]
    assert len(PdfReader(ROOT / source["path"]).pages) == source["pdf_pages"] == 350
    stored_manifest = json.loads((ROOT / source["integrity_manifest_path"]).read_text())
    stored = stored_manifest["raw/official/600036.SH_2025.pdf"]
    http = json.loads((ROOT / source["http_metadata_path"]).read_text())
    filing = json.loads((ROOT / source["metadata_path"]).read_text())
    assert stored["bytes"] == http["bytes"] == len(report_bytes)
    assert stored["sha256"] == http["sha256"] == filing["pdf_sha256"] == source["sha256"]
    assert http["url"] == filing["url"] == source["url"]
    assert filing["chosen"]["announcementId"] == source["disclosure_id"]


def test_dates_preserve_recorded_precision_without_inventing_public_instant() -> None:
    source = MANIFEST["source"]
    http = json.loads((ROOT / source["http_metadata_path"]).read_text())
    filing = json.loads((ROOT / source["metadata_path"]).read_text())
    assert source["published_date"] == "2026-03-28"
    assert source["published_precision"] == "date"
    assert source["published_at"] is None
    assert f'finalpage/{source["published_date"]}/' in filing["chosen"]["adjunctUrl"]
    assert source["acquired_at"] == http["fetched_at_utc"]
    assert source["acquired_precision"] == "timestamp"
    assert source["original_ref"] is None


@pytest.mark.parametrize("locator_id", tuple(MANIFEST["locators"]))
def test_exact_excerpt_and_printed_page_from_original_bytes(
    report_bytes: bytes, locator_id: str,
) -> None:
    locator = MANIFEST["locators"][locator_id]
    text = bank_page_text(report_bytes, locator["pdf_page"])
    assert text.splitlines()[2] == locator["printed_page"]
    assert locator["excerpt"] in canonical(text)
    assert sha256(locator["excerpt"].encode()).hexdigest() == locator["excerpt_sha256"]


@pytest.mark.parametrize("fact", EXPECTED["facts"], ids=lambda fact: fact["id"])
def test_facts_bind_exact_annual_row_column_unit_and_group(
    report_bytes: bytes, fact: dict[str, Any],
) -> None:
    text = canonical(bank_page_text(report_bytes, fact["pdf_page"]))
    locator = MANIFEST["locators"][fact["locator_id"]]
    assert fact["printed_page"] == locator["printed_page"]
    assert fact["source_token"] == fact["raw_value"]
    if fact["pdf_page"] == 22:
        assert "本集团资产负债项目" in text
        assert "2025 年 2024 年" in text
        assert "（人民币百万元，百分比除外）" in text
        nii_row = "净利息收入 ／ 215,593 ／ ／ 211,277 ／"
        nim_row = "净利息收益率 ／ ／ 1.87 ／ ／ 1.98"
        assert nii_row in text and nim_row in text
        row = nii_row if fact["metric"] == "nii" else nim_row
        cells = row.split()[1:]
        column = 1 if fact["metric"] == "nii" else 2
        if fact["period_key"] == "FY2024":
            column += 3
        assert cells[column] == fact["raw_value"]
        assert fact["column_path"].startswith("2025 年" if column < 3 else "2024 年")
    else:
        # The quarterly table follows this annual decomposition on the same page.
        annual = text.split("下表列出所示期间本集团资产负债项目", 1)[0]
        assert "本集团由于规模变化和利率变化" in annual
        assert "2025 年对比2024 年" in annual
        assert "（人民币百万元） 规模 利率 增 （减） 净值" in annual
        assert "净利息收入变动 16,798 (12,482) 4,316" in annual
        columns = {"nii_volume_effect": "规模", "nii_rate_effect": "利率",
                   "nii_change_disclosed": "净值"}
        assert fact["column_path"].endswith(columns[fact["metric"]])
    raw = Decimal(fact["raw_value"].replace(",", "").replace("(", "-").replace(")", ""))
    multiplier = Decimal("1000000") if fact["raw_unit"] == "CNY_million" else Decimal("0.01")
    assert raw * multiplier == Decimal(fact["value"])


def test_source_scope_period_and_interaction_rule_are_explicit(report_bytes: bytes) -> None:
    scope = MANIFEST["scope"]
    group = canonical(bank_page_text(report_bytes, 3))
    accounting = canonical(bank_page_text(report_bytes, 143))
    decomposition = canonical(bank_page_text(report_bytes, 23))
    assert "本集团 ： 招商银行及其子公司" in group
    assert "本集团执行中华人民共和国财政部" in accounting
    assert "企业会计准则" in accounting
    assert "会计年度自公历1 月1日起至12月 31日止。" in accounting
    assert scope["reporting_scope"] == "consolidated"
    assert scope["accounting_standard"] == "CAS"
    assert scope["period"] == {"start": "2025-01-01", "end": "2025-12-31",
                               "basis": "period_to_date/annual"}
    assert scope["baseline_period"] == {"start": "2024-01-01", "end": "2024-12-31",
                                        "basis": "period_to_date/annual"}
    # PDF line wrapping splits “规模变化” after 变; compare the prose rule
    # without presentation whitespace while the hashed excerpt retains it.
    assert MANIFEST["decomposition_rule"] in decomposition.replace(" ", "")


def test_derived_expectations_use_decimal_and_no_balancing_item() -> None:
    values = {fact["id"]: Decimal(fact["value"]) for fact in EXPECTED["facts"]}
    derived = EXPECTED["derived"]
    change = values["nii_2025"] - values["nii_2024"]
    assert change == values["nii_change_disclosed"] == Decimal(derived["nii_change_cny"])
    residual = change - values["nii_volume_effect"] - values["nii_rate_effect"]
    assert residual == Decimal(derived["decomposition_residual_cny"]) == 0
    with localcontext() as context:
        context.prec = 28
        growth = change / values["nii_2024"]
        assert growth == Decimal(derived["nii_yoy_fraction"])
        assert (growth * 100).quantize(Decimal("0.01")) == Decimal(derived["nii_yoy_display_percent"])
    assert (values["nim_2025"] - values["nim_2024"]) * 10000 == Decimal("-11")
    assert derived["nim_change_basis_points"] == "-11"
