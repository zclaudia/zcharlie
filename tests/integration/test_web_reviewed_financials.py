"""HK2-07 reviewed financial view contract as rendered by the real web routes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from freezegun import freeze_time

from investment_agent.materials.contracts import Verification
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.web.routes import _financial_timestamp
from tests.integration.test_api_research_materials import seed_listings, store_hsbc_material

TODAY = "2026-10-12"


def test_financial_timestamps_render_utc_seconds_and_dates_stay_date_only() -> None:
    assert _financial_timestamp(datetime(2026, 10, 15, 4, 3, 2, 987654, tzinfo=UTC)) == (
        "2026-10-15 04:03:02 UTC"
    )
    assert _financial_timestamp(date(2026, 10, 15)) == "2026-10-15"
    assert _financial_timestamp(None) == "未知"


def _login(client) -> None:
    response = client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert response.status_code == 200


async def test_review_times_metric_evidence_and_source_state_match_read_model(
    api_client, tmp_path
) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    _login(api_client)

    with freeze_time(TODAY):
        response = api_client.get(
            f"/research/listings/{seeded['HKEX/00005']}/financial-context"
        )
        page = api_client.get("/app/stocks/00005.HK")
    assert response.status_code == 200
    assert page.status_code == 200
    view = response.json()

    freshness = view["freshness"]
    assert set(freshness) == {
        "last_requested_at",
        "last_attempt_at",
        "last_success_at",
        "last_imported_at",
        "last_verified_on",
        "latest_period_end",
        "latestness",
        "latestness_checked_on",
        "sources",
    }
    assert freshness["latestness"] == "unknown"
    assert freshness["latestness_checked_on"] is None
    assert freshness["last_verified_on"] is None
    assert freshness["last_imported_at"] is None
    assert set(freshness["sources"]) == {"hk_annual_reports", "us_issuer_facts"}
    assert all(source["enabled"] is False for source in freshness["sources"].values())
    assert all(source["state"] == "disabled" for source in freshness["sources"].values())
    assert view["capabilities"]["online_refresh"] is False

    # Per-metric coverage is part of this same real view and distinguishes a
    # non-applicable metric from absent or unverified evidence.
    metric_coverage = view["metric_coverage"]
    assert set(metric_coverage) == {item["key"] for item in view["metrics"]}
    for item in metric_coverage.values():
        assert item["evidence_status"] in {
            "not_applicable", "missing", "candidate", "verified", "mixed"
        }
        assert item["usable_facts"] >= item["verified_facts"] >= 0

    html = page.text
    assert 'data-latestness="unknown"' in html
    assert '<dd id="financial-last-verified-on">未知</dd>' in html
    assert '<dd id="financial-latestness-checked-on">未知</dd>' in html
    assert '<dd id="financial-last-imported-at">未知</dd>' in html
    assert "来源已关闭" in html
    assert "使用许可：未确认" in html
    assert "最近核对原件日期" in html and "资料实际更新" in html
    assert "最新披露核对至" in html
    assert "资料更新时间表示导入时间，两者不会互相替代" in html
    assert "最新状态未核实" in html
    if view["metrics"]:
        assert 'class="financial-metric-evidence"' in html
        assert "可用事实" in html and "已核对" in html


async def test_derived_verified_value_is_labeled_as_calculation(api_client, tmp_path) -> None:
    from tests.integration.test_api_research_materials import HSBC, fixture_facts, store_material

    seeded = await seed_listings()
    reported = next(
        item
        for item in fixture_facts(HSBC, "FY2024")
        if item.metric == "revenue_net_credit"
    )
    derived = replace(
        reported,
        verification=Verification.VERIFIED,
        original_locator="financial.reconciliation.sha256:deadbeef",
        provenance=replace(reported.provenance, source="material.equity_reconciliation"),
    )
    await store_material(
        tmp_path,
        listing_id=seeded["HKEX/00005"],
        company_key=HSBC,
        labels=("FY2024",),
        extra=(derived,),
    )
    _login(api_client)
    response = api_client.get(
        f"/research/listings/{seeded['HKEX/00005']}/financial-context"
    )
    page = api_client.get("/app/stocks/00005.HK")
    assert response.status_code == page.status_code == 200
    view = response.json()
    assert any(
        fact["evidence_kind"] == "derived_calculation" and fact["source"] == "复核计算"
        for fact in view["facts"]
    )
    section = page.text[page.text.index('id="stock-financials"'):]
    assert 'data-evidence-kind="derived_calculation"' in section
    assert "计算已复核" in section
    assert "复核计算" in section
    assert "查看计算依据" in section
    assert "financial.reconciliation.sha256:deadbeef" not in section.split("查看计算依据</summary>")[0]


async def test_empty_listing_shows_unknown_dates_and_does_not_claim_a_refresh(api_client) -> None:
    seeded = await seed_listings()
    _login(api_client)

    with freeze_time(TODAY):
        response = api_client.get(
            f"/research/listings/{seeded['NASDAQ/AAPL']}/financial-context"
        )
        page = api_client.get("/app/stocks/AAPL")
    assert response.status_code == 200
    assert page.status_code == 200
    view = response.json()
    assert view["facts"] == []
    assert view["metrics"] == []
    assert view["freshness"]["last_verified_on"] is None
    assert view["freshness"]["last_imported_at"] is None
    assert view["freshness"]["latestness"] == "unknown"
    assert view["refresh"]["run_id"] is None

    html = page.text
    assert '<dd id="financial-last-verified-on">未知</dd>' in html
    assert '<dd id="financial-last-imported-at">未知</dd>' in html
    assert '<dd id="financial-last-requested-at">未知</dd>' in html
    assert "尚无资料刷新任务记录" in html
    assert "在线刷新未开启" in html
    assert "不会联网采集" in html
    assert "资料待采集" in html


async def test_refresh_post_then_read_only_poll_keeps_queued_state_honest(api_client) -> None:
    seeded = await seed_listings()
    _login(api_client)
    listing_id = seeded["NASDAQ/AAPL"]
    context_url = f"/research/listings/{listing_id}/financial-context"

    before = api_client.get(context_url)
    assert before.status_code == 200
    previous_success = before.json()["freshness"]["last_success_at"]
    queued = api_client.post(
        f"/research/listings/{listing_id}/refresh",
        json={},
        headers={"X-Requested-With": "fetch"},
    )
    assert queued.status_code == 202
    assert queued.json()["online_refresh_enabled"] is False

    # The same safe GET the page polls reports the actual lifecycle. It does not
    # update the last-success time or infer a fetch from the POST response.
    current = api_client.get(context_url)
    assert current.status_code == 200
    view = current.json()
    assert view["refresh"]["run_id"] == queued.json()["run_id"]
    assert view["refresh"]["status"] == "queued"
    assert view["refresh"]["reason"] == "user_refresh"
    assert view["freshness"]["last_success_at"] == previous_success
    assert view["capabilities"]["online_refresh"] is False
    assert all(item["enabled"] is False for item in view["freshness"]["sources"].values())

    page = api_client.get("/app/stocks/AAPL")
    assert page.status_code == 200
    html = page.text
    assert 'id="financial-current-run-id"' in html
    assert 'id="financial-current-run-status">排队中</dd>' in html
    assert 'id="financial-current-run-reason">用户点击刷新</dd>' in html
    assert "来源已关闭" in html
    assert "在线刷新未开启" in html
    assert "成功采集" not in html

    # A completed partial run has its own copy; it is not shown as a success
    # and it still cannot turn the disabled sources on or advance last success.
    run_id = queued.json()["run_id"]
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        run.status = "partial"
        run.stage = "partial"
        run.finished_at = datetime.now(UTC)
        run.error_summary = "disabled source"

    partial = api_client.get(context_url)
    assert partial.status_code == 200
    partial_view = partial.json()
    assert partial_view["refresh"]["status"] == "partial"
    assert partial_view["freshness"]["last_success_at"] == previous_success
    assert all(item["enabled"] is False for item in partial_view["freshness"]["sources"].values())
    partial_page = api_client.get("/app/stocks/AAPL")
    assert partial_page.status_code == 200
    assert 'id="financial-current-run-status">部分完成</dd>' in partial_page.text
    assert "部分来源未完成" in partial_page.text
    assert "成功采集" not in partial_page.text
