"""Provider contract regression tests (work package A4).

The ifind-proxy response → canonical conversion must replay offline: every
sanitized ``provider_raw.json`` fixture is decoded back into
``IfindProxyResult`` objects and pushed through
``canonicalize_ifind_results`` with the fixture's own retrieval timestamp,
then compared byte-for-byte against the frozen canonical inputs. Synthetic
bodies cover the error and missing-data boundaries of every provider
endpoint shape.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from investment_agent.research.ifind_chain import (
    IfindProxyClient,
    IfindProxyResult,
    canonicalize_ifind_results,
    parse_proxy_rows,
    plan_ifind_requests,
    validate_explanation_policy,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"


def _manifest() -> dict[str, Any]:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _decode_raw(raw: dict[str, Any]) -> dict[str, IfindProxyResult]:
    results: dict[str, IfindProxyResult] = {}
    for name, entry in raw["requests"].items():
        body = entry["body"]
        results[name] = IfindProxyResult(
            path=str(entry["path"]),
            body=body,
            rows=parse_proxy_rows(body),
        )
    return results


@pytest.mark.parametrize(
    "sample",
    [
        s
        for s in _manifest()["samples"]
        if s["fixture_status"] == "sanitized"
        and (FIXTURE_ROOT / s["ticker"].split(".")[0] / "provider_raw.json").is_file()
    ],
    ids=lambda sample: sample["ticker"],
)
def test_provider_fixture_replays_to_frozen_canonical(sample: dict[str, Any]):
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    raw = json.loads((fixture_dir / "provider_raw.json").read_text(encoding="utf-8"))
    frozen_pool = json.loads((fixture_dir / "pool.json").read_text(encoding="utf-8"))
    frozen_market = json.loads((fixture_dir / "market.json").read_text(encoding="utf-8"))
    frozen_financial = json.loads((fixture_dir / "financial.json").read_text(encoding="utf-8"))

    bundle = canonicalize_ifind_results(
        ticker=sample["ticker"],
        industry=sample["industry"],
        report_year=int(_manifest()["report_year"]),
        results=_decode_raw(raw),
        retrieved_at=str(raw["retrieved_at"]),
        as_of=date.fromisoformat(str(sample["collected_at"])),
    )

    assert bundle.pool == frozen_pool
    assert bundle.market == frozen_market
    assert bundle.financial == frozen_financial
    assert bundle.raw == raw


def test_provider_fixture_decodes_without_errors_for_every_sample():
    for sample in _manifest()["samples"]:
        if sample["fixture_status"] != "sanitized":
            continue
        fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
        if not (fixture_dir / "provider_raw.json").is_file():
            continue
        raw = json.loads((fixture_dir / "provider_raw.json").read_text(encoding="utf-8"))
        results = _decode_raw(raw)
        assert set(results) == set(plan_ifind_requests(ticker=sample["ticker"], report_year=2025, as_of=date(2026, 10, 5))), sample["ticker"]


def test_plan_requests_cover_every_provider_endpoint():
    requests = plan_ifind_requests(ticker="600406.SH", report_year=2025, as_of=date(2026, 10, 5))
    assert set(requests) == {
        "info",
        "history",
        "income_latest",
        "income_base",
        "cash_latest",
        "capital_latest",
        "profitability_2022",
        "profitability_2023",
        "profitability_2024",
        "profitability_2025",
    }
    paths = {path for path, _payload in requests.values()}
    assert paths == {"/stock/info", "/stock/history", "/financial/statements", "/financial/index"}


def test_plan_requests_rejects_invalid_ticker():
    with pytest.raises(ValueError, match="ticker must look like"):
        plan_ifind_requests(ticker="600406", report_year=2025, as_of=date(2026, 10, 5))


def test_parse_proxy_rows_header_only_csv_yields_no_rows():
    body = {"format": "json", "data": {"data_preview": "code,close\n"}}
    assert parse_proxy_rows(body) == []


def test_parse_proxy_rows_blank_string_yields_no_rows():
    body = {"format": "json", "data": {"data_preview": "   \n"}}
    assert parse_proxy_rows(body) == []


def test_parse_proxy_rows_rejects_unknown_data_shape():
    with pytest.raises(ValueError, match="Unsupported iFinD proxy data shape"):
        parse_proxy_rows({"format": "xml", "data": None})


def test_parse_proxy_rows_accepts_direct_dict_and_list_rows():
    assert parse_proxy_rows({"data": {"a": "1"}}) == [{"a": "1"}]
    assert parse_proxy_rows({"data": [{"a": "1"}, {"b": None}, "ignored"]}) == [
        {"a": "1"},
        {"b": ""},
    ]


def _minimal_history_body(rows: int = 3) -> dict[str, Any]:
    csv_lines = ["time,close"]
    for index in range(rows):
        csv_lines.append(f"2026100{index + 1},{10 + index}")
    return {"format": "json", "data": {"data_preview": "\n".join(csv_lines) + "\n"}}


def _minimal_info_body(total_shares: str = "1000000") -> dict[str, Any]:
    return {
        "format": "json",
        "data": {
            "data_preview": (
                "ths_stock_short_name_stock,ths_total_shares_stock\n"
                f"测试股份,{total_shares}\n"
            )
        },
    }


def _minimal_profitability_body(**values: str) -> dict[str, Any]:
    fields = [
        "ths_eps_basic_stock",
        "ths_nav_ps_stock",
        "ths_revenue_ps_stock",
        "ths_roe_stock",
        "ths_gross_selling_rate_stock",
        "ths_net_sales_rate_stock",
    ]
    header = ",".join(fields)
    row = ",".join(values.get(field, "") for field in fields)
    return {"format": "json", "data": {"data_preview": f"{header}\n{row}\n"}}


def _minimal_statement_body(**values: str) -> dict[str, Any]:
    fields = ["ths_operating_total_revenue_stock", "ths_np_atoopc_stock", "ths_ncf_from_oa_stock", "ths_cash_paid_for_assets_stock"]
    header = ",".join(fields)
    row = ",".join(values.get(field, "") for field in fields)
    return {"format": "json", "data": {"data_preview": f"{header}\n{row}\n"}}


def _minimal_capital_body(debt_ratio: str = "50.0") -> dict[str, Any]:
    return {
        "format": "json",
        "data": {"data_preview": f"ths_asset_liab_ratio_stock\n{debt_ratio}\n"},
    }


def _synthetic_results(**overrides: dict[str, Any]) -> dict[str, IfindProxyResult]:
    bodies: dict[str, dict[str, Any]] = {
        "info": _minimal_info_body(),
        "history": _minimal_history_body(),
        "income_latest": _minimal_statement_body(),
        "income_base": _minimal_statement_body(),
        "cash_latest": _minimal_statement_body(),
        "capital_latest": _minimal_capital_body(),
        **{f"profitability_{year}": _minimal_profitability_body() for year in range(2022, 2026)},
    }
    for name, body in overrides.items():
        bodies[name] = body
    return {
        name: IfindProxyResult(path="/synthetic", body=body, rows=parse_proxy_rows(body))
        for name, body in bodies.items()
    }


def test_canonicalize_marks_missing_fields_for_review_and_keeps_nulls():
    bundle = canonicalize_ifind_results(
        ticker="600406.SH",
        industry="电力设备",
        report_year=2025,
        results=_synthetic_results(),
        retrieved_at="2026-10-05T00:00:00+00:00",
    )

    market_item = bundle.market["items"][0]
    financial_item = bundle.financial["items"][0]
    stock_item = bundle.pool["items"][0]

    # Unknown numeric values stay null and never become zero or guessed data.
    assert market_item["latest_price"] == 12.0
    assert market_item["pe"] is None and market_item["pb"] is None and market_item["ps"] is None
    assert financial_item["revenue"] is None and financial_item["net_profit"] is None
    assert stock_item["name"] == "测试股份"

    assert "dividend_yield" in market_item["missing_fields"]
    assert {"pe", "pb", "ps"} <= set(market_item["missing_fields"])
    assert financial_item["missing_fields"], "empty statements must mark missing fields"
    assert financial_item["needs_agent_review"] is True
    assert any(
        flag["flag"] == "missing_field" and flag["severity"] == "review"
        for flag in financial_item["quality_flags"]
    )


def test_canonicalize_computes_derived_metrics_when_inputs_present():
    results = _synthetic_results(
        income_latest=_minimal_statement_body(
            ths_operating_total_revenue_stock="1000",
            ths_np_atoopc_stock="100",
        ),
        income_base=_minimal_statement_body(
            ths_operating_total_revenue_stock="500",
            ths_np_atoopc_stock="25",
        ),
        cash_latest=_minimal_statement_body(
            ths_ncf_from_oa_stock="120",
            ths_cash_paid_for_assets_stock="20",
        ),
        capital_latest=_minimal_capital_body(debt_ratio="40"),
        **{
            f"profitability_{year}": _minimal_profitability_body(
                ths_roe_stock="10", ths_eps_basic_stock="0.5"
            )
            for year in range(2022, 2026)
        },
    )

    bundle = canonicalize_ifind_results(
        ticker="600406.SH",
        industry="电力设备",
        report_year=2025,
        results=results,
        retrieved_at="2026-10-05T00:00:00+00:00",
    )

    financial_item = bundle.financial["items"][0]
    market_item = bundle.market["items"][0]
    assert financial_item["revenue"] == 1000.0
    assert financial_item["free_cash_flow"] == 100.0
    assert financial_item["revenue_growth_3y"] == pytest.approx(0.259921, abs=1e-6)
    assert financial_item["net_profit_growth_3y"] == pytest.approx(0.587401, abs=1e-6)
    assert financial_item["roe_stability"] == 1.0
    assert financial_item["debt_ratio"] == pytest.approx(0.4)
    assert market_item["pe"] == pytest.approx(24.0)
    assert market_item["market_cap"] == pytest.approx(12.0 * 1_000_000)


def test_canonicalize_rejects_empty_history_rows():
    empty_history = {"format": "json", "data": {"data_preview": "time,close\n"}}
    with pytest.raises(ValueError, match="no daily price history"):
        canonicalize_ifind_results(
            ticker="600406.SH",
            industry="电力设备",
            report_year=2025,
            results=_synthetic_results(history=empty_history),
            retrieved_at="2026-10-05T00:00:00+00:00",
        )


def test_canonicalize_rejects_endpoint_with_zero_rows():
    empty_profitability = {"format": "json", "data": {"data_preview": "ths_roe_stock\n"}}
    with pytest.raises(ValueError, match="no rows for profitability"):
        canonicalize_ifind_results(
            ticker="600406.SH",
            industry="电力设备",
            report_year=2025,
            results=_synthetic_results(profitability_2025=empty_profitability),
            retrieved_at="2026-10-05T00:00:00+00:00",
        )


@respx.mock
@pytest.mark.asyncio
async def test_post_rows_rejects_non_object_proxy_response():
    respx.post("http://proxy.test/stock/info").mock(return_value=httpx.Response(200, json=[1, 2]))
    async with IfindProxyClient(base_url="http://proxy.test", api_key="k") as client:
        with pytest.raises(ValueError, match="expected object"):
            await client.post_rows("/stock/info", {"ticker": "600406.SH"})


@respx.mock
@pytest.mark.asyncio
async def test_post_rows_propagates_proxy_error_status():
    respx.post("http://proxy.test/stock/history").mock(
        return_value=httpx.Response(502, json={"detail": "iFinD 返回错误"})
    )
    async with IfindProxyClient(base_url="http://proxy.test", api_key="k") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_rows("/stock/history", {"ticker": "600406.SH"})


@respx.mock
@pytest.mark.asyncio
async def test_post_rows_decodes_object_and_dict_rows():
    respx.post("http://proxy.test/stock/info").mock(
        return_value=httpx.Response(200, json={"data": {"a": "1"}})
    )
    async with IfindProxyClient(base_url="http://proxy.test", api_key="k") as client:
        result = await client.post_rows("/stock/info", {"ticker": "600406.SH"})
    assert result.rows == [{"a": "1"}]
    assert result.path == "/stock/info"


def test_explanation_policy_requires_needs_review_label():
    from investment_agent.research.ifind_chain import EvidenceClaim, ResearchExplanation

    score_item = {"needs_agent_review": True, "business_score": 50.0}
    explanation = ResearchExplanation(
        summary="summary",
        classification_rationale=[EvidenceClaim(text="c", supporting_fields=["business_score"])],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion="normal",
    )

    with pytest.raises(ValueError, match="needs_review workflow label"):
        validate_explanation_policy(explanation, score_item)

    explanation.workflow_conclusion = "needs_review: dividend yield missing"
    validate_explanation_policy(explanation, score_item)
