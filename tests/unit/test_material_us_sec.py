"""SEC CompanyFacts + Submissions adapter tests (plan 04).

Fixtures under ``tests/fixtures/materials/us/**`` are trimmed samples of the
saved research responses
(``research/market-data-validation/evidence/remaining-final-03/raw/us`` for
AAPL/COST/JPM/XOM/TSM, captured 2026-10-11, and
``.../validated-run-01/raw/sec`` for the LNC 2023 case). Only the taxonomies,
concepts, units and filings these tests exercise were kept; ``accn``/``filed``/
``form``/``fy``/``fp``/``start``/``end``/``val``/``frame`` are retained,
``frame`` only to prove that deleting it changes nothing. Each fixture's
``filings.files`` list is trimmed to the shards shipped next to it.

Day-count convention: ``FactPeriod.days`` is frozen by
``materials/contracts.py`` as ``(end - start).days`` (the end day is not counted
twice), while a fiscal year's true length counts both endpoints. AAPL's FY2023
is 53 weeks = 371 inclusive days (370 stored), FY2024 is 52 weeks = 364
inclusive days (363 stored); the assertions check both forms so the 52/53-week
distinction can never be flattened into 365.

The LNC regression cases mirror ``validation.md`` §"SEC 原始和修订申报" and the
saved ``derived/sec/cutoffs.json``; the adapter itself holds no EdgarTools
dependency, and the module docstring records that these tests must be re-run if
a third-party parser is ever introduced.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from investment_agent.domain.security import (
    ControlledIdentity,
    SecurityType,
    SourceBinding,
)
from investment_agent.materials.contracts import (
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    FactPeriod,
    PeriodBasis,
    RawPayload,
    validate_draft,
)
from investment_agent.materials.us_sec import (
    ADAPTER_VERSION,
    COMPANYFACTS_URL,
    DEFAULT_PERIOD_WINDOW,
    MIN_INTERVAL_SECONDS,
    MISSING_TAG_REASON,
    SHARD_URL,
    SOURCE_FACTS,
    SOURCE_FILINGS,
    SUBMISSIONS_URL,
    USER_AGENT,
    SecFactsAdapter,
    _discover_periods,
    _period_references,
    _read_facts_document,
    _read_submissions_document,
    _target_periods,
)
from investment_agent.materials.us_sec_concepts import _choose_taxonomy
from investment_agent.materials.us_sec_periods import (
    PERIOD_NOT_FILED,
    PERIOD_STATES,
    FactRow,
    FilingRef,
    PeriodSelection,
    UnreliableNotice,
    select_period_state,
)
from tests.unit._hk_us_support import pilot_identities

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "us"
EVIDENCE = Path(
    "/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/evidence"
)
LNC_CUTOFFS = EVIDENCE / "validated-run-02" / "derived" / "sec" / "cutoffs.json"
FETCHED_AT = datetime(2026, 10, 11, 15, 0, tzinfo=UTC)


# --- helpers ----------------------------------------------------------------


def identity(ticker: str) -> ControlledIdentity:
    for candidate in pilot_identities():
        if candidate.ticker == ticker:
            return candidate
    raise AssertionError(f"{ticker} is not in the pilot list")


LNC_IDENTITY = ControlledIdentity(
    market_code="NYSE",
    ticker="LNC",
    company_key="lincoln-national",
    company_name="Lincoln National Corporation",
    company_name_zh="林肯国民",
    security_type=SecurityType.ORDINARY,
    quote_currency="USD",
    report_currency="USD",
    fiscal_year_end_note="12-31",
    sources=(SourceBinding(source=SOURCE_FACTS, symbol="59558"),),
    valid_from=date(2026, 10, 12),
)


def raw_document(path: Path) -> RawPayload:
    return RawPayload(
        name=path.name,
        content=path.read_bytes(),
        media_type="application/json",
        fetched_at=FETCHED_AT,
        source_url="https://data.sec.gov/saved-evidence",
        http_status=200,
    )


def fixture_paths(ticker: str) -> list[Path]:
    root = FIXTURES / ticker
    return [root / "companyfacts.json", root / "submissions.json", *sorted(root.glob("shards/*.json"))]


def raw_documents(ticker: str) -> list[RawPayload]:
    return [raw_document(path) for path in fixture_paths(ticker)]


def replace_document(documents: list[RawPayload], name: str, content: bytes) -> None:
    for index, document in enumerate(documents):
        if document.name == name:
            documents[index] = RawPayload(
                name=document.name,
                content=content,
                media_type="application/json",
                fetched_at=document.fetched_at,
                source_url=document.source_url,
                http_status=document.http_status,
            )
            return
    raise AssertionError(f"{name} is not among the documents")


def mutate_json(documents: list[RawPayload], name: str, mutate) -> None:
    document = next(item for item in documents if item.name == name)
    body = mutate(json.loads(document.content))
    replace_document(documents, name, json.dumps(body).encode())


def parse_ticker(
    ticker: str,
    *,
    periods: tuple[FactPeriod, ...] = (),
    raw: list[RawPayload] | None = None,
):
    adapter = SecFactsAdapter(subject_id=7)
    resolved = LNC_IDENTITY if ticker == "LNC" else identity(ticker)
    documents = raw_documents(ticker) if raw is None else raw
    return adapter.parse(resolved, raw=documents, periods=periods)


def annual(drafts: tuple[DatasetDraft, ...], end: date) -> DatasetDraft:
    for draft in drafts:
        if (
            draft.kind == DatasetKind.ANNUAL_FACTS
            and draft.period is not None
            and draft.period.end == end
        ):
            return draft
    raise AssertionError(f"no annual_facts draft ending {end}")


def filing_index(drafts: tuple[DatasetDraft, ...]) -> DatasetDraft:
    for draft in drafts:
        if draft.kind == DatasetKind.FILING_INDEX:
            return draft
    raise AssertionError("no filing_index draft")


def fact(period_draft: DatasetDraft, metric: str) -> dict[str, Any]:
    entries = facts(period_draft, metric)
    assert entries, f"{metric} is absent from the draft"
    return entries[0]


def facts(period_draft: DatasetDraft, metric: str) -> list[dict[str, Any]]:
    return [entry for entry in period_draft.payload["facts"] if entry["metric"] == metric]


def strip_frames(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: strip_frames(value) for key, value in node.items() if key != "frame"}
    if isinstance(node, list):
        return [strip_frames(item) for item in node]
    return node


# --- module surface ---------------------------------------------------------


def test_module_constants_match_the_plan():
    assert SOURCE_FACTS == "sec.companyfacts.v1"
    assert SOURCE_FILINGS == "sec.submissions.v1"
    assert ADAPTER_VERSION == "1"
    assert COMPANYFACTS_URL.format(cik=320193) == (
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    )
    assert SUBMISSIONS_URL.format(cik=19617) == (
        "https://data.sec.gov/submissions/CIK0000019617.json"
    )
    assert SHARD_URL.format(name="CIK0000019617-submissions-007.json") == (
        "https://data.sec.gov/submissions/CIK0000019617-submissions-007.json"
    )
    assert USER_AGENT.startswith("zcharlie-research/0.1")
    assert MIN_INTERVAL_SECONDS == 0.5
    assert DEFAULT_PERIOD_WINDOW == 4
    assert PERIOD_STATES == (
        "current",
        "superseded",
        "nonreliance_notice_known",
        "conflict",
    )
    assert PERIOD_NOT_FILED not in PERIOD_STATES


def test_requests_use_the_identity_cik_and_cover_the_historical_shards():
    adapter = SecFactsAdapter(subject_id=1)
    first = adapter.requests(identity("JPM"), periods=())
    assert [request.name for request in first] == ["companyfacts.json", "submissions.json"]
    assert first[0].url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000019617.json"
    assert first[1].url == "https://data.sec.gov/submissions/CIK0000019617.json"

    adapter.parse(identity("JPM"), raw=raw_documents("JPM"))
    second = adapter.requests(identity("JPM"), periods=())
    listed = json.loads((FIXTURES / "JPM" / "submissions.json").read_text())["filings"]["files"]
    assert [request.name for request in second] == [
        "companyfacts.json",
        "submissions.json",
        *[entry["name"] for entry in listed],
    ]
    assert second[2].url == "https://data.sec.gov/submissions/CIK0000019617-submissions-007.json"


def test_aapl_fixture_ships_no_shard_so_requests_stay_at_two():
    """The trimmed fixture lists no shard (see the module docstring), and none is asked for."""

    assert json.loads((FIXTURES / "AAPL" / "submissions.json").read_text())["filings"]["files"] == []
    adapter = SecFactsAdapter(subject_id=1)
    adapter.parse(identity("AAPL"), raw=raw_documents("AAPL"))
    assert [request.name for request in adapter.requests(identity("AAPL"), periods=())] == [
        "companyfacts.json",
        "submissions.json",
    ]


def test_identity_without_an_sec_binding_is_rejected():
    adapter = SecFactsAdapter(subject_id=1)
    with pytest.raises(ValueError, match="binds no sec.companyfacts.v1"):
        adapter.requests(identity("00002"), periods=())
    with pytest.raises(ValueError, match="binds no sec.companyfacts.v1"):
        adapter.parse(identity("00002"), raw=[])


def test_subject_id_must_be_supplied_somewhere():
    adapter = SecFactsAdapter()
    with pytest.raises(ValueError, match="subject_id"):
        adapter.parse(identity("AAPL"), raw=raw_documents("AAPL"))
    result = adapter.parse(identity("AAPL"), raw=raw_documents("AAPL"), subject_id=42)
    assert {draft.subject_id for draft in result.drafts} == {42}


# --- fiscal years -----------------------------------------------------------


def test_aapl_52_53_week_years_keep_their_true_lengths():
    result = parse_ticker("AAPL")
    assert result.problems == ()
    fy2023 = annual(result.drafts, date(2023, 9, 30))
    fy2024 = annual(result.drafts, date(2024, 9, 28))
    assert fy2023.period is not None and fy2024.period is not None
    assert (fy2023.period.start, fy2023.period.end) == (date(2022, 9, 25), date(2023, 9, 30))
    assert (fy2024.period.start, fy2024.period.end) == (date(2023, 10, 1), date(2024, 9, 28))
    # 53 weeks and 52 weeks, in both the stored (exclusive) and the inclusive count
    assert fy2023.period.days == 370 and (fy2023.period.days + 1) // 7 == 53
    assert fy2024.period.days == 363 and (fy2024.period.days + 1) // 7 == 52
    assert fy2023.period.days != 365 and fy2024.period.days != 365
    assert fy2023.period.label == "FY2023" and fy2024.period.label == "FY2024"
    assert fy2023.period.end != date(2023, 12, 31)
    assert fy2024.period.end != date(2024, 12, 31)
    # the period's own fiscal boundaries equal the issuer's reportDate
    candidates = fy2024.payload["period_state"]["candidates"]
    assert candidates and all(
        candidate["report_period"] == "2024-09-28"
        for candidate in candidates
        if candidate["accession"] == "0000320193-24-000123"
    )


def test_cost_fiscal_year_is_not_a_calendar_year():
    result = parse_ticker("COST")
    assert result.problems == ()
    fy2023 = annual(result.drafts, date(2023, 9, 3))
    fy2024 = annual(result.drafts, date(2024, 9, 1))
    for draft in (fy2023, fy2024):
        assert draft.period is not None
        assert draft.period.end != date(draft.period.end.year, 12, 31)
        assert 364 <= draft.period.days + 1 <= 371
    assert fy2023.period is not None and fy2023.period.days == 370
    assert fy2024.period is not None and fy2024.period.days == 363


def test_requested_periods_filter_the_produced_fiscal_years():
    requested = (
        FactPeriod(start=date(2023, 10, 1), end=date(2024, 9, 28), basis=PeriodBasis.ANNUAL),
    )
    result = parse_ticker("AAPL", periods=requested)
    produced = [
        draft.period.end
        for draft in result.drafts
        if draft.kind == DatasetKind.ANNUAL_FACTS and draft.period is not None
    ]
    assert produced == [date(2024, 9, 28)]


def test_default_period_window_keeps_the_most_recent_fiscal_years():
    rows = [
        FactRow(
            taxonomy="us-gaap",
            concept="NetIncomeLoss",
            unit="USD",
            start=date(year, 1, 1),
            end=date(year, 12, 31),
            value=str(year),
            accession=f"0000000000-{year}-000001",
            form="10-K",
            filed=date(year + 1, 2, 1),
            fy=year,
            fp="FY",
        )
        for year in range(2019, 2025)
    ]
    assert len(_discover_periods(rows)) == 6
    assert [period.end for period in _target_periods(rows, ())] == [
        date(2021, 12, 31),
        date(2022, 12, 31),
        date(2023, 12, 31),
        date(2024, 12, 31),
    ]


# --- fact values ------------------------------------------------------------


def test_aapl_facts_carry_units_currencies_and_decimal_strings():
    result = parse_ticker("AAPL")
    fy2024 = annual(result.drafts, date(2024, 9, 28))
    expected = {
        "revenue_operating": ("391035000000", "USD", "USD"),
        "net_profit": ("93736000000", "USD", "USD"),
        "total_assets": ("364980000000", "USD", "USD"),
        "total_liabilities": ("308030000000", "USD", "USD"),
        "equity_parent": ("56950000000", "USD", "USD"),
        "operating_cash_flow": ("118254000000", "USD", "USD"),
        "capex_ppe": ("9447000000", "USD", "USD"),
        "eps_basic": ("6.11", "USD/share", "USD"),
        "eps_diluted": ("6.08", "USD/share", "USD"),
        "shares_weighted_basic": ("15343783000", "shares", None),
        "shares_outstanding_end": ("15116786000", "shares", None),
    }
    for metric, (raw_value, unit, currency) in expected.items():
        entries = facts(fy2024, metric)
        assert len(entries) == 1, metric
        entry = entries[0]
        assert entry["value"]["raw_value"] == raw_value
        assert entry["value"]["unit"] == unit
        assert entry["value"]["currency"] == currency
        assert isinstance(entry["value"]["raw_value"], str)
        assert entry["source_field"].startswith("us-gaap:")
        assert entry["value"]["scale"] == 1
        assert entry["value"]["sign_convention"] == "as_reported"
    assert fact(fy2024, "revenue_operating")["source_field"] == (
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert fact(fy2024, "eps_basic")["semantics"]["per_share_basis"] == "basic"
    assert fact(fy2024, "eps_diluted")["semantics"]["per_share_basis"] == "diluted"
    assert fact(fy2024, "eps_basic")["value"]["raw_unit"] == "USD/shares"
    assert fact(fy2024, "equity_parent")["source_field"] == "us-gaap:StockholdersEquity"


def test_cost_profit_parent_binds_the_consolidated_profit_concept():
    result = parse_ticker("COST")
    fy2024 = annual(result.drafts, date(2024, 9, 1))
    assert fact(fy2024, "net_profit")["value"]["raw_value"] == "7367000000"
    assert fact(fy2024, "net_profit")["source_field"] == "us-gaap:NetIncomeLoss"
    profit_parent = fact(fy2024, "profit_parent")
    assert profit_parent["source_field"] == "us-gaap:ProfitLoss"
    assert profit_parent["value"]["raw_value"] == "7367000000"
    # us-gaap:ProfitLoss includes non-controlling interests, and the fact says so
    assert profit_parent["semantics"]["equity_scope"] == "total"


def test_jpm_missing_capex_is_null_with_a_reason_not_a_nearby_tag():
    result = parse_ticker("JPM")
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    entry = fact(fy2024, "capex_ppe")
    assert entry["value"]["raw_value"] is None
    assert entry["missing_reason"] == MISSING_TAG_REASON
    assert entry["applicability"] == "unknown"
    assert entry["source_field"] is None and entry["source_label"] is None
    assert "capex_ppe" in fy2024.payload["missing_metrics"]
    assert all(
        candidate["source_field"] is None or "Payments" not in candidate["source_field"]
        for candidate in fy2024.payload["facts"]
    )
    source = json.loads((FIXTURES / "JPM" / "companyfacts.json").read_text())
    assert "PaymentsToAcquirePropertyPlantAndEquipment" not in source["facts"]["us-gaap"]


def test_jpm_net_profit_and_revenue_use_the_standard_gaap_figures():
    result = parse_ticker("JPM")
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    net_profit = fact(fy2024, "net_profit")
    assert net_profit["value"]["raw_value"] == "58471000000"
    assert net_profit["source_field"] == "us-gaap:NetIncomeLoss"
    assert net_profit["semantics"]["accounting_standard"] == "us_gaap"
    assert fact(fy2024, "revenue_operating")["value"]["raw_value"] == "177556000000"
    assert fact(fy2024, "revenue_operating")["source_field"] == "us-gaap:Revenues"
    # JPM's `RevenuesNetOfInterestExpense` is the same value but is not mapped
    assert fy2024.payload["unmapped"]["concepts"]["us-gaap"] > 0


def test_xom_revenue_operating_binding_is_the_standard_revenues_concept():
    result = parse_ticker("XOM")
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    revenue = fact(fy2024, "revenue_operating")
    assert revenue["source_field"] == "us-gaap:Revenues"
    assert revenue["source_label"] == "Revenues"
    assert revenue["value"]["raw_value"] == "349585000000"
    assert "339247000000" not in json.dumps(fy2024.payload)


def test_xom_sales_only_tag_does_not_become_revenue_operating():
    """Negative case: a pure sales tag must not be accepted as operating revenue."""

    def mutate(body: dict[str, Any]) -> dict[str, Any]:
        concepts = body["facts"]["us-gaap"]
        node = concepts.pop("Revenues")
        for rows in node["units"].values():
            for row in rows:
                row["val"] = 339247000000
        concepts["SalesRevenueNet"] = node
        return body

    documents = raw_documents("XOM")
    mutate_json(documents, "companyfacts.json", mutate)
    result = parse_ticker("XOM", raw=documents)
    assert result.problems == ()
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    revenue = fact(fy2024, "revenue_operating")
    assert revenue["value"]["raw_value"] is None
    assert revenue["missing_reason"] == MISSING_TAG_REASON
    assert "339247000000" not in json.dumps(fy2024.payload)
    assert fy2024.payload["unmapped"]["concepts"]["us-gaap"] > 0


def test_xom_parent_profit_and_net_profit_stay_distinct():
    result = parse_ticker("XOM")
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    assert fact(fy2024, "net_profit")["value"]["raw_value"] == "33680000000"
    assert fact(fy2024, "net_profit")["source_field"] == "us-gaap:NetIncomeLoss"
    assert fact(fy2024, "profit_parent")["value"]["raw_value"] == "35063000000"
    assert fact(fy2024, "profit_parent")["source_field"] == "us-gaap:ProfitLoss"


# --- IFRS / ADS -------------------------------------------------------------


def test_tsm_is_ifrs_twd_without_any_usd_conversion():
    result = parse_ticker("TSM")
    assert result.problems == ()
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    assert fy2024.payload["accounting_standard"] == "ifrs"
    assert fy2024.payload["report_currency"] == "TWD"
    expected = {
        "revenue_operating": "2894307700000",
        "net_profit": "1157523900000",
        "profit_parent": "1158380200000",
        "total_assets": "6691764700000",
        "total_liabilities": "2412493100000",
        "equity_parent": "4244266500000",
        "operating_cash_flow": "1826177100000",
        "capex_ppe": "956006500000",
        "eps_basic": "44.68",
        "eps_diluted": "44.67",
        "shares_weighted_basic": "25927600000",
    }
    for metric, raw_value in expected.items():
        entry = fact(fy2024, metric)
        assert entry["value"]["raw_value"] == raw_value, metric
        assert entry["source_field"].startswith("ifrs-full:"), metric
        assert entry["semantics"]["accounting_standard"] == "ifrs"
    for entry in fy2024.payload["facts"]:
        if entry["value"]["raw_value"] is None:
            continue
        if entry["metric"] == "shares_weighted_basic":
            assert entry["value"]["unit"] == "shares"
            assert entry["value"]["currency"] is None
        else:
            assert entry["value"]["unit"] in {"TWD", "TWD/share"}
            assert entry["value"]["currency"] == "TWD"
    # the USD convenience translations are counted as skipped and never appear
    assert fy2024.payload["unmapped"]["unit_skipped"]["USD"] > 0
    assert "88268000000" not in json.dumps(fy2024.payload)
    assert "35327200000" not in json.dumps(fy2024.payload)
    assert "USD" not in {entry["value"]["currency"] for entry in fy2024.payload["facts"]}


def test_tsm_profit_metrics_only_read_the_20f_series():
    result = parse_ticker("TSM")
    fy2024 = annual(result.drafts, date(2024, 12, 31))
    for metric in ("net_profit", "profit_parent"):
        entry = fact(fy2024, metric)
        assert entry["provenance"]["accession"] == "0001193125-25-083423"
        assert "6-K" not in (entry["source_version"] or "")
    source = json.loads((FIXTURES / "TSM" / "companyfacts.json").read_text())
    six_k_rows = [
        row
        for concept in source["facts"]["ifrs-full"].values()
        for rows in concept["units"].values()
        for row in rows
        if row["form"] == "6-K"
    ]
    assert six_k_rows, "the fixture must carry a real 6-K row to prove it is ignored"


def test_non_annual_forms_never_define_a_fiscal_year():
    def mutate(body: dict[str, Any]) -> dict[str, Any]:
        for concept in body["facts"]["ifrs-full"].values():
            for rows in concept["units"].values():
                for row in rows:
                    if row["form"] == "20-F":
                        row["form"] = "6-K"
        return body

    documents = raw_documents("TSM")
    mutate_json(documents, "companyfacts.json", mutate)
    result = parse_ticker("TSM", raw=documents)
    assert [draft for draft in result.drafts if draft.kind == DatasetKind.ANNUAL_FACTS] == []
    assert filing_index(result.drafts).payload["periods"] == {}


# --- selection rules --------------------------------------------------------


def test_frame_fields_are_never_used():
    fixture = json.loads((FIXTURES / "AAPL" / "companyfacts.json").read_text())
    stripped = strip_frames(fixture)
    assert '"frame"' in json.dumps(fixture) and "frame" not in json.dumps(stripped)
    baseline = parse_ticker("AAPL")
    documents = raw_documents("AAPL")
    replace_document(documents, "companyfacts.json", json.dumps(stripped).encode())
    without_frames = parse_ticker("AAPL", raw=documents)
    assert without_frames.problems == ()
    assert [draft.period for draft in without_frames.drafts] == [
        draft.period for draft in baseline.drafts
    ]
    assert [draft.payload["facts"] for draft in without_frames.drafts] == [
        draft.payload["facts"] for draft in baseline.drafts
    ]


def test_instant_and_duration_facts_never_mix():
    result = parse_ticker("AAPL")
    fy2024 = annual(result.drafts, date(2024, 9, 28))
    for metric in ("total_assets", "total_liabilities", "equity_parent", "shares_outstanding_end"):
        period = fact(fy2024, metric)["period"]
        assert period["start"] is None, metric
        assert period["basis"] == "instant", metric
        assert period["days"] is None, metric
        assert period["end"] == "2024-09-28", metric
    for metric in ("revenue_operating", "net_profit", "operating_cash_flow", "eps_basic"):
        period = fact(fy2024, metric)["period"]
        assert period["start"] == "2023-10-01", metric
        assert period["basis"] == "annual", metric
        assert period["days"] == 363, metric


def test_publication_precision_follows_the_source_fields():
    aapl = parse_ticker("AAPL")
    provenance = fact(annual(aapl.drafts, date(2024, 9, 28)), "revenue_operating")["provenance"]
    assert provenance["source_published_precision"] == DatePrecision.TIMESTAMP.value
    assert provenance["accepted_at"] == "2024-11-01T14:01:36+00:00"
    assert provenance["source_published_at"] == provenance["accepted_at"]
    assert provenance["source_published_date"] == "2024-11-01"
    assert provenance["source_available_at"] is None

    lnc = parse_ticker("LNC")
    lnc_draft = annual(lnc.drafts, date(2022, 12, 31))
    by_accession = {
        entry["provenance"]["accession"]: entry["provenance"]
        for entry in facts(lnc_draft, "net_profit")
    }
    assert by_accession["0000059558-23-000009"]["source_published_precision"] == "timestamp"
    assert by_accession["0000059558-23-000019"]["accepted_at"] == "2023-03-30T21:13:26+00:00"
    # the re-citation that wins the dedupe is the later 8-K, which Submissions dates
    assert by_accession["0001562762-23-000261"]["source_published_precision"] == "timestamp"
    assert by_accession["0001562762-23-000261"]["source_published_date"] == "2023-05-22"
    # accessions that only exist in CompanyFacts carry the filing date alone; they
    # stay visible as candidates with no acceptance time
    fact_only = {
        candidate["accession"]: candidate
        for candidate in lnc_draft.payload["period_state"]["candidates"]
        if candidate["accession"] in {"0000059558-25-000023", "0001193125-26-157983"}
    }
    assert set(fact_only) == {"0000059558-25-000023", "0001193125-26-157983"}
    assert all(candidate["accepted_at"] is None for candidate in fact_only.values())
    for draft in lnc.drafts:
        for entry in draft.payload["facts"]:
            assert entry["provenance"]["source_available_at"] is None


def test_payloads_are_deterministic_and_do_not_back_fill_fetch_times():
    first = parse_ticker("COST")
    second = parse_ticker("COST")
    assert [draft.payload for draft in first.drafts] == [draft.payload for draft in second.drafts]
    assert all(draft.captured_at.tzinfo is not None for draft in first.drafts)
    assert len({draft.captured_at for draft in first.drafts}) == 1
    assert all(draft.source_fetched_at is None for draft in first.drafts)
    assert all(draft.raw_bytes is None for draft in first.drafts)


@pytest.mark.parametrize("ticker", ["AAPL", "COST", "JPM", "XOM", "TSM", "LNC"])
def test_every_draft_passes_the_frozen_validator(ticker):
    result = parse_ticker(ticker)
    assert result.drafts
    for draft in result.drafts:
        assert validate_draft(draft) == ()


def test_unmapped_taxonomies_and_concepts_are_counted_without_producing_facts():
    result = parse_ticker("AAPL")
    fy2024 = annual(result.drafts, date(2024, 9, 28))
    unmapped = fy2024.payload["unmapped"]
    assert unmapped["taxonomies"]["dei"] >= 1
    assert unmapped["concepts"]["us-gaap"] >= 1
    assert all(
        entry["source_field"] is None or not entry["source_field"].startswith("dei:")
        for entry in fy2024.payload["facts"]
    )
    assert fact(fy2024, "shares_outstanding_end")["source_field"] == (
        "us-gaap:CommonStockSharesOutstanding"
    )


# --- the version state machine ---------------------------------------------


def _lnc_filings() -> tuple[FilingRef, ...]:
    period_end = date(2022, 12, 31)
    return (
        FilingRef(
            accession="0000059558-23-000009",
            form="10-K",
            filing_date=date(2023, 2, 16),
            accepted_at=datetime(2023, 2, 16, 21, 19, 28, tzinfo=UTC),
            report_period=period_end,
            primary_document="lnc-20221231x10k.htm",
            period_end=period_end,
            observations=(("net_profit", "-2227000000"),),
        ),
        FilingRef(
            accession="0000059558-23-000019",
            form="10-K/A",
            filing_date=date(2023, 3, 30),
            accepted_at=datetime(2023, 3, 30, 21, 13, 26, tzinfo=UTC),
            report_period=period_end,
            primary_document="lnc-20221231x10ka.htm",
            period_end=period_end,
            observations=(("net_profit", "-2241000000"),),
        ),
        FilingRef(
            accession="0001562762-23-000261",
            form="8-K",
            filing_date=date(2023, 5, 22),
            accepted_at=datetime(2023, 5, 22, 20, 11, 58, tzinfo=UTC),
            report_period=date(2023, 5, 22),
            primary_document="lnc-20230522x8k.htm",
            period_end=period_end,
            observations=(("net_profit", "1358000000"),),
        ),
    )


LNC_NOTICE = UnreliableNotice(
    accession="0001193125-23-080586",
    form="8-K",
    accepted_at=datetime(2023, 3, 27, 21, 5, 57, tzinfo=UTC),
    items=("2.02", "4.02", "7.01"),
    known_at=datetime(2023, 3, 27, 21, 5, 57, tzinfo=UTC),
)


@pytest.mark.parametrize(
    ("as_of", "status", "accession", "value", "usable"),
    [
        ("2023-02-16T21:19:27Z", PERIOD_NOT_FILED, None, None, False),
        ("2023-02-16T21:19:28Z", "current", "0000059558-23-000009", "-2227000000", True),
        ("2023-03-01T00:00:00Z", "current", "0000059558-23-000009", "-2227000000", True),
        (
            "2023-03-27T21:05:57Z",
            "nonreliance_notice_known",
            "0000059558-23-000009",
            "-2227000000",
            False,
        ),
        (
            "2023-03-30T21:13:25Z",
            "nonreliance_notice_known",
            "0000059558-23-000009",
            "-2227000000",
            False,
        ),
        ("2023-03-30T21:13:26Z", "current", "0000059558-23-000019", "-2241000000", True),
        ("2023-04-01T00:00:00Z", "current", "0000059558-23-000019", "-2241000000", True),
    ],
)
def test_lnc_state_machine_sees_the_documented_time_points(
    as_of, status, accession, value, usable
):
    cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    selection = select_period_state(_lnc_filings(), (LNC_NOTICE,), as_of=cutoff)
    assert selection.status == status
    assert selection.selected_accession == accession
    assert selection.value_for("net_profit") == value
    assert selection.usable is usable


@pytest.mark.skipif(not LNC_CUTOFFS.exists(), reason="saved SEC evidence is not present")
def test_lnc_state_machine_matches_the_saved_cutoffs_file():
    cutoffs = json.loads(LNC_CUTOFFS.read_text())
    assert len(cutoffs) == 7
    status_map = {
        "not_yet_filed": PERIOD_NOT_FILED,
        "as_reported_available": "current",
        "nonreliance_notice_known": "nonreliance_notice_known",
    }
    for entry in cutoffs:
        cutoff = datetime.fromisoformat(entry["cutoff_utc"].replace("Z", "+00:00"))
        selection = select_period_state(_lnc_filings(), (LNC_NOTICE,), as_of=cutoff)
        assert selection.status == status_map[entry["status"]], entry["cutoff_utc"]
        selected = entry["selected"]
        if selected is None:
            assert selection.selected_accession is None
        else:
            assert selection.selected_accession == selected["accn"]
            assert selection.value_for("net_profit") == str(selected["val"])
        if "usable" in entry:
            assert selection.usable is entry["usable"]


def test_lnc_revision_replaces_the_original_without_using_is_restated():
    selection = select_period_state(_lnc_filings(), (LNC_NOTICE,))
    # with the whole material known, the later 8-K re-citation makes the period
    # ambiguous; every candidate is kept and the revision is still the current one
    assert selection.status == "conflict"
    assert selection.usable is False
    assert selection.selected_accession == "0000059558-23-000019"
    assert selection.value_for("net_profit") == "-2241000000"
    states = {(candidate.accession, candidate.state) for candidate in selection.candidates}
    assert ("0000059558-23-000009", "superseded") in states
    assert ("0000059558-23-000019", "current") in states
    assert ("0001562762-23-000261", "conflict") in states

    # the machine has no is_restated input at all: it orders versions by
    # acceptance time only, so an earlier amendment does not replace anything
    assert not hasattr(_lnc_filings()[1], "is_restated")
    early_amendment = FilingRef(
        accession="0000059558-22-000999",
        form="10-K/A",
        filing_date=date(2023, 1, 5),
        accepted_at=datetime(2023, 1, 5, 12, 0, tzinfo=UTC),
        report_period=date(2022, 12, 31),
        primary_document="lnc-20221231x10ka-early.htm",
        period_end=date(2022, 12, 31),
        observations=(("net_profit", "-2227000000"),),
    )
    early = select_period_state((_lnc_filings()[0], early_amendment), notices=())
    assert early.status == "current"
    assert early.selected_accession == "0000059558-23-000009"


def test_lnc_conflict_keeps_every_candidate_and_computes_no_growth():
    result = parse_ticker("LNC")
    assert result.problems == ()
    draft = annual(result.drafts, date(2022, 12, 31))
    entries = facts(draft, "net_profit")
    assert [entry["value"]["raw_value"] for entry in entries] == [
        "-2241000000",
        "-2227000000",
        "1358000000",
    ]
    assert all(entry["conflict"] == "unresolved" for entry in entries)
    assert all(entry["conflict_note"] for entry in entries)
    assert {entry["provenance"]["accession"] for entry in entries} >= {
        "0000059558-23-000019",
        "0000059558-23-000009",
        "0001562762-23-000261",
    }
    state = draft.payload["period_state"]
    assert state["status"] == "conflict"
    assert state["selected_accession"] == "0000059558-23-000019"
    assert state["usable"] is False
    assert state["note"] is not None and "no overwrite" in state["note"]
    assert not any("growth" in key for key in draft.payload)
    assert not any("ratio" in key for key in draft.payload)


def test_superseded_period_keeps_the_old_value_when_the_amendment_has_no_facts():
    references = []
    for reference in _lnc_filings()[:2]:
        if reference.accession == "0000059558-23-000019":
            reference = FilingRef(
                accession=reference.accession,
                form=reference.form,
                filing_date=reference.filing_date,
                accepted_at=reference.accepted_at,
                report_period=reference.report_period,
                primary_document=reference.primary_document,
                period_end=reference.period_end,
                observations=(),
            )
        references.append(reference)
    selection = select_period_state(tuple(references), ())
    assert selection.status == "superseded"
    assert selection.selected_accession == "0000059558-23-000009"
    assert selection.value_for("net_profit") == "-2227000000"
    assert selection.usable is False
    assert selection.note is not None


def test_filing_without_any_date_counts_as_known_at_every_cutoff():
    reference = FilingRef(
        accession="0000000000-99-000001",
        form="10-K",
        filing_date=None,
        accepted_at=None,
        report_period=date(2022, 12, 31),
        primary_document=None,
        period_end=date(2022, 12, 31),
        observations=(("net_profit", "-1"),),
    )
    selection = select_period_state((reference,), (), as_of=datetime(2000, 1, 1, tzinfo=UTC))
    assert isinstance(selection, PeriodSelection)
    assert selection.status == "current"
    assert selection.selected_accession == "0000000000-99-000001"


def test_period_without_its_own_annual_report_is_not_filed():
    citation = _lnc_filings()[2]
    selection = select_period_state((citation,), ())
    assert selection.status == PERIOD_NOT_FILED
    assert selection.selected_accession is None
    assert [candidate.state for candidate in selection.candidates] == ["conflict"]


# --- submissions, shards and error structures --------------------------------


def test_jpm_historical_shards_are_merged_into_the_filing_index():
    result = parse_ticker("JPM")
    assert result.problems == ()
    index = filing_index(result.drafts)
    assert index.source == SOURCE_FILINGS
    assert index.period is None
    assert index.payload["facts"] == []
    assert index.payload["shards"] == [
        "CIK0000019617-submissions-007.json",
        "CIK0000019617-submissions-035.json",
    ]
    accessions = {entry["accession"] for entry in index.payload["filings"]}
    assert "0000019617-22-000272" in accessions
    assert "0000019617-25-000270" in accessions
    early = next(
        entry
        for entry in index.payload["filings"]
        if entry["accession"] == "0000019617-22-000272"
    )
    assert early["report_period"] == "2021-12-31"
    assert early["accepted_at"] == "2022-02-23T02:56:31+00:00"
    jpm = annual(result.drafts, date(2024, 12, 31))
    assert fact(jpm, "total_assets")["provenance"]["accepted_at"] == "2025-02-15T02:17:07+00:00"


def test_a_listed_shard_that_was_not_fetched_is_reported_but_not_fatal():
    documents = [
        document
        for document in raw_documents("JPM")
        if not document.name.startswith("CIK0000019617-submissions-")
    ]
    result = parse_ticker("JPM", raw=documents)
    assert "missing_shard: CIK0000019617-submissions-007.json" in result.problems
    jpm = annual(result.drafts, date(2024, 12, 31))
    assert fact(jpm, "total_assets")["provenance"]["source_published_precision"] == "date"
    assert fact(jpm, "total_assets")["provenance"]["accepted_at"] is None


def test_html_body_is_a_structural_problem():
    documents = raw_documents("AAPL")
    replace_document(documents, "companyfacts.json", b"<html><body>rate limited</body></html>")
    result = parse_ticker("AAPL", raw=documents)
    assert "not_json: companyfacts.json" in result.problems
    assert result.drafts == ()


def test_missing_facts_block_is_a_structural_problem():
    documents = raw_documents("AAPL")
    mutate_json(
        documents,
        "companyfacts.json",
        lambda body: {key: value for key, value in body.items() if key != "facts"},
    )
    result = parse_ticker("AAPL", raw=documents)
    assert "missing_facts: companyfacts.json" in result.problems
    assert result.drafts == ()


def test_companyfacts_cik_mismatch_is_a_structural_problem():
    documents = raw_documents("AAPL")

    def mutate(body: dict[str, Any]) -> dict[str, Any]:
        body["cik"] = 320194
        return body

    mutate_json(documents, "companyfacts.json", mutate)
    result = parse_ticker("AAPL", raw=documents)
    assert any(problem.startswith("cik_mismatch: companyfacts.json") for problem in result.problems)
    assert result.drafts == ()


def test_submissions_cik_mismatch_is_a_structural_problem():
    documents = raw_documents("AAPL")

    def mutate(body: dict[str, Any]) -> dict[str, Any]:
        body["cik"] = "0000320194"
        return body

    mutate_json(documents, "submissions.json", mutate)
    result = parse_ticker("AAPL", raw=documents)
    assert any(problem.startswith("cik_mismatch: submissions.json") for problem in result.problems)
    assert result.drafts == ()


def test_broken_shard_shape_is_a_structural_problem():
    documents = raw_documents("JPM")
    replace_document(documents, "CIK0000019617-submissions-007.json", b'{"foo": []}')
    result = parse_ticker("JPM", raw=documents)
    assert "unexpected_shape: CIK0000019617-submissions-007.json" in result.problems
    assert result.drafts == ()


def test_missing_documents_are_reported_but_partial_material_is_still_published():
    only_facts = [
        document for document in raw_documents("COST") if document.name == "companyfacts.json"
    ]
    facts_result = parse_ticker("COST", raw=only_facts)
    assert "missing_document: submissions.json" in facts_result.problems
    assert [draft.kind for draft in facts_result.drafts] == [DatasetKind.ANNUAL_FACTS] * 2
    cost = annual(facts_result.drafts, date(2024, 9, 1))
    revenue = fact(cost, "revenue_operating")
    assert revenue["value"]["raw_value"] == "254453000000"
    # only `filed` is known without Submissions: DATE precision, no timestamp
    assert revenue["provenance"]["source_published_precision"] == DatePrecision.DATE.value
    assert revenue["provenance"]["source_published_at"] is None
    assert revenue["provenance"]["accepted_at"] is None
    assert revenue["provenance"]["source_published_date"] == "2024-10-09"
    assert cost.payload["period_state"]["status"] == PERIOD_NOT_FILED

    only_submissions = [
        document for document in raw_documents("COST") if document.name == "submissions.json"
    ]
    index_result = parse_ticker("COST", raw=only_submissions)
    assert "missing_document: companyfacts.json" in index_result.problems
    assert [draft.kind for draft in index_result.drafts] == [DatasetKind.FILING_INDEX]
    assert filing_index(index_result.drafts).payload["periods"] == {}

    # a shard without its Submissions document is reported, not silently dropped
    jpm_facts = [
        document
        for document in raw_documents("JPM")
        if document.name == "companyfacts.json"
    ]
    shard = next(
        document
        for document in raw_documents("JPM")
        if document.name == "CIK0000019617-submissions-007.json"
    )
    orphan = parse_ticker("JPM", raw=[*jpm_facts, shard])
    assert "unexpected_document: CIK0000019617-submissions-007.json" in orphan.problems


def test_a_requested_fiscal_year_missing_from_the_response_is_reported():
    requested = (
        FactPeriod(start=date(2019, 1, 1), end=date(2019, 12, 31), basis=PeriodBasis.ANNUAL),
    )
    result = parse_ticker("AAPL", periods=requested)
    assert "missing_period: 2019-12-31" in result.problems
    assert [draft for draft in result.drafts if draft.kind == DatasetKind.ANNUAL_FACTS] == []


def test_filing_index_payload_shape():
    result = parse_ticker("COST")
    payload = filing_index(result.drafts).payload
    assert payload["cik"] == "0000909832"
    assert set(payload) >= {
        "facts",
        "cik",
        "filings",
        "shards",
        "periods",
        "unreliable_notices",
    }
    first = payload["filings"][0]
    assert set(first) == {
        "accession",
        "form",
        "filing_date",
        "accepted_at",
        "report_period",
        "primary_document",
        "is_amendment",
    }
    period = payload["periods"]["2024-09-01"]
    assert period["status"] == "current"
    assert period["usable"] is True
    assert period["selected_accession"] == "0000909832-24-000049"
    assert set(period["candidates"][0]) == {
        "accession",
        "form",
        "state",
        "usable",
        "accepted_at",
        "report_period",
    }


def test_unreliable_notices_are_exposed_from_the_submissions_document():
    result = parse_ticker("LNC")
    index = filing_index(result.drafts)
    notices = index.payload["unreliable_notices"]
    assert [notice["accession"] for notice in notices] == ["0001193125-23-080586"]
    assert notices[0]["items"] == ["2.02", "4.02", "7.01"]
    assert notices[0]["known_at"] == "2023-03-27T21:05:57+00:00"
    assert index.payload["periods"]["2022-12-31"]["status"] == "conflict"


def test_reader_helpers_work_on_their_own():
    documents = raw_documents("AAPL")
    fatal: list[str] = []
    soft: list[str] = []
    facts_document = _read_facts_document(documents[0], "0000320193", fatal, soft)
    assert facts_document is not None and fatal == []
    submissions = _read_submissions_document("0000320193", documents[1], (), fatal, soft)
    assert submissions is not None and submissions.filing_count == 5
    assert submissions.merged_shards == () and submissions.missing_shards == ()
    period = _discover_periods(facts_document.rows)[0]
    references = _period_references(
        facts_document.rows, submissions.filings, period, "us-gaap", ("net_profit",)
    )
    assert references
    assert _choose_taxonomy(facts_document.rows, "USD") == "us-gaap"


def test_annual_drafts_keep_the_subject_source_and_adapter_version():
    result = parse_ticker("AAPL")
    annual_drafts = [draft for draft in result.drafts if draft.kind == DatasetKind.ANNUAL_FACTS]
    assert len(annual_drafts) == 2
    for draft in annual_drafts:
        assert draft.source == SOURCE_FACTS
        assert draft.subject_kind.value == "company"
        assert draft.subject_id == 7
        assert draft.schema_version == 1
        assert draft.adapter_version == ADAPTER_VERSION
        assert draft.raw_source_url == (
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
        )


def test_an_empty_facts_block_is_not_an_error():
    documents = raw_documents("AAPL")

    def mutate(body: dict[str, Any]) -> dict[str, Any]:
        body["facts"] = {}
        return body

    mutate_json(documents, "companyfacts.json", mutate)
    result = parse_ticker("AAPL", raw=documents)
    assert result.problems == ()
    assert [draft.kind for draft in result.drafts] == [DatasetKind.FILING_INDEX]
    index = filing_index(result.drafts)
    assert index.payload["periods"] == {}
    assert index.payload["annual_filing_count"] == 3
