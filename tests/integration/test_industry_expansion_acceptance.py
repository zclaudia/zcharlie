"""Independent acceptance of the two-industry offline chain and its boundaries."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.peer import PeerAnnualSnapshot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PERIOD = date(2025, 12, 31)
BANK = "601398.SH"
AUTH = {"X-API-Key": "industry-acceptance"}


@pytest_asyncio.fixture
async def industry_chain(tmp_path, monkeypatch):
    database = f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}"
    for key, value in {
        "DATABASE_URL": database,
        "DATA_DIR": str(tmp_path),
        "API_KEY": "industry-acceptance",
        "LLM_TIER3_API_KEY": "test-only",
        "INDUSTRY_PEER_CONTEXT_ENABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    async with engine_lifespan(database) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield tmp_path


async def import_both(data_dir):
    import json

    from investment_agent.cli.commands.bank_annual import import_bank_annual_snapshots
    from investment_agent.cli.commands.peer_annual import (
        import_peer_annual_snapshots,
        load_audit_json,
        load_sample_json,
    )
    from investment_agent.cli.commands.peer_cohort import import_sample_roster
    from investment_agent.industry_peers.batch import submit_offline_batch
    from investment_agent.storage.repositories.listing import ListingRepository

    acquired = datetime.now(UTC) - timedelta(hours=1)
    bank_root = FIXTURES / "bank_run02"
    bank_sample = json.loads((bank_root / "sample.json").read_text())
    baijiu_root = FIXTURES / "peer_run02"
    baijiu = load_sample_json(baijiu_root / "derived/baijiu-peer-sample.json")
    async with session_scope() as session:
        listings = ListingRepository(session)
        for ticker in bank_sample["bank"]:
            await listings.ensure_a_share_listing(ticker, company_name=f"银行样本 {ticker}")
        for company in baijiu["companies"]:
            await listings.ensure_a_share_listing(company["code"], company_name=company["name"])
        await import_sample_roster(
            session, sample_path=bank_root / "sample.json", industry="bank",
            cohort_key="bank_current", acquired_at=acquired,
        )
        await import_bank_annual_snapshots(
            session, evidence_root=bank_root, report_period=PERIOD, acquired_at=acquired,
        )
        # A second import must reuse the saved facts even with a different time.
        await import_bank_annual_snapshots(
            session, evidence_root=bank_root, report_period=PERIOD,
            acquired_at=acquired + timedelta(minutes=1),
        )
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == 10
        await import_sample_roster(
            session, sample_path=baijiu_root / "sample.json", acquired_at=acquired,
        )
        await import_peer_annual_snapshots(
            session, sample=baijiu,
            audit=load_audit_json(baijiu_root / "derived/baijiu-official-audit.json"),
            acquired_at=acquired, originals_root=None,
        )
    async with session_scope() as session:
        bank_batch = await submit_offline_batch(
            session, data_dir=data_dir, cohort_key="bank_current", report_period=PERIOD,
        )
    async with session_scope() as session:
        baijiu_batch = await submit_offline_batch(session, data_dir=data_dir, report_period=PERIOD)
    return bank_batch.mapping.batch_run_id, baijiu_batch.mapping.batch_run_id


async def test_two_industries_preserve_values_bindings_and_candidate_limits(industry_chain):
    from investment_agent.api.app import create_app
    from investment_agent.industry_peers.metric_packs import BAIJIU_PACK, BANK_PACK
    from investment_agent.industry_peers.resolver import PeerContextResolver
    from investment_agent.industry_peers.service import (
        get_industry_peer_context,
        peer_context_binding_from,
    )
    from investment_agent.investor.foundations import InvestorError

    bank_batch, baijiu_batch = await import_both(industry_chain)
    client = TestClient(create_app())
    assert client.get("/industries").status_code == 401
    listing = client.get("/industries", headers=AUTH)
    assert listing.status_code == 200, listing.text
    assert {item["metric_pack"] for item in listing.json()["industries"]} == {BAIJIU_PACK, BANK_PACK}
    response = client.get(f"/stocks/{BANK}/industry-peer-context", headers=AUTH)
    assert response.status_code == 200, response.text
    bank = response.json()
    assert bank["metric_pack"] == BANK_PACK
    assert bank["batch_run_id"] == bank_batch
    metrics = {item["metric_key"]: item for item in bank["metrics"]}
    assert set(metrics) == {"roe", "net_interest_margin", "npl_ratio", "provision_coverage"}
    expected = {
        "net_interest_margin": ("0.0128", "0.0174"),
        "npl_ratio": ("0.0131", "0.0094"),
        "provision_coverage": ("2.136", "3.1362"),
    }
    for key, (target, peer_median) in expected.items():
        metric = metrics[key]
        assert Decimal(metric["target_value"]["value"]) == Decimal(target)
        assert Decimal(metric["peer_median"]["value"]) == Decimal(peer_median)
        assert metric["valid_peer_count"] == metric["candidate_peer_count"] == 9
        assert metric["verification_counts"].get("verified", 0) == 0
    assert metrics["roe"]["peer_median"] is None
    assert metrics["roe"]["ascending_position"] is None

    industry = client.get("/industries/bank_current", headers=AUTH)
    assert industry.status_code == 200, industry.text
    overview = industry.json()
    by_key = {item["metric_key"]: item for item in overview["metrics"]}
    assert overview["candidate_count"] == 10
    assert by_key["net_interest_margin"]["valid_count"] == 10
    assert Decimal(by_key["net_interest_margin"]["median"]) == Decimal("0.01735")
    # The industry distribution includes the anchor; peers exclude it.
    assert by_key["net_interest_margin"]["median"] != by_key["net_interest_margin"]["target_excluded_median"]
    for key in expected:
        values = [Decimal(item["value"]) for item in overview["companies"] if item["metric_key"] == key and item["included"]]
        assert len(values) == 10
        assert Decimal(by_key[key]["median"]) == median(values)

    white = client.get("/stocks/600519.SH/industry-peer-context", headers=AUTH).json()
    assert white["batch_run_id"] == baijiu_batch
    white_metrics = {item["metric_key"]: item for item in white["metrics"]}
    assert Decimal(white_metrics["roe"]["target_value"]["value"]) == Decimal("0.3253")
    assert Decimal(white_metrics["roe"]["peer_median"]["value"]) == Decimal("0.0739")
    assert set(white_metrics) == {"roe", "contract_revenue", "cfo_parent_profit"}
    assert client.get(f"/industries/bank_current?batch_run_id={baijiu_batch}", headers=AUTH).status_code == 404

    async with session_scope() as session:
        result = await get_industry_peer_context(session, ticker=BANK, data_dir=industry_chain)
        binding = peer_context_binding_from(result)
        assert binding is not None and binding.metric_pack == BANK_PACK
        ref = next(metric.calculation_ref for metric in result.metrics if metric.metric_key == "npl_ratio")
        assert ref is not None
        resolver = PeerContextResolver(session, binding=binding, data_dir=industry_chain)
        validation = await resolver.verify(ref)
        assert not validation.inputs_verified
        with pytest.raises(InvestorError):
            await resolver.verify(ref, require_verified_inputs=True)
        forged = binding.model_copy(update={"metric_pack": BAIJIU_PACK})
        with pytest.raises(InvestorError):
            await PeerContextResolver(session, binding=forged, data_dir=industry_chain).verify(ref)
        assert await session.scalar(select(func.count()).select_from(PipelineRun).where(PipelineRun.kind == "industry_peer_collect")) == 0


async def test_real_worker_reads_the_frozen_bank_pack_with_a_fake_model(industry_chain):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    from investment_agent.agent.research import ResearchOutput
    from investment_agent.investor.common import ObjectRef
    from investment_agent.investor.enums import TaskStatus
    from investment_agent.storage.models.agent import ExecutionGrantRow
    from investment_agent.storage.repositories.listing import ListingRepository
    from tests.support.conversations import base, message, new_conversation, submit
    from tests.support.research import progress, response, run

    bank_batch, _ = await import_both(industry_chain)
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(BANK)
        assert listing is not None
        target = ObjectRef(kind="listing", id=listing.id, revision=1)
    settings = base(industry_peer_context_enabled=True, data_dir=industry_chain)
    conversation = await new_conversation()
    receipt = await submit(
        conversation, message(text="Explain the saved bank sample", context_refs=(target,)),
        settings=settings,
    )
    async with session_scope() as session:
        grant = await session.get(ExecutionGrantRow, str(receipt.task.id))
        assert grant is not None
        assert grant.payload["peer_context"]["batch_run_id"] == bank_batch
        assert grant.payload["peer_context"]["metric_pack"] == "bank_annual_v1"
        expected_refs = {
            item["metric_key"]: item["ref"]
            for item in grant.payload["peer_context"]["metric_calculation_refs"]
        }
    calls = 0
    observed = {}

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                [ToolCallPart("get_industry_peer_context", {"ticker": BANK}, tool_call_id="bank")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
            )
        for item in messages:
            for part in item.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "get_industry_peer_context":
                    observed.update(part.content)
        return response(ResearchOutput(progress=progress()))

    task = await run(receipt, FunctionModel(model), settings=settings)
    assert task.status is TaskStatus.SUCCEEDED
    assert observed["status"] == "ok"
    metrics = {item["metric_key"]: item for item in observed["data"]["metrics"]}
    assert set(metrics) == {"roe", "net_interest_margin", "npl_ratio", "provision_coverage"}
    assert metrics["roe"]["peer_median"] is None
    assert Decimal(metrics["net_interest_margin"]["target_value"]["value"]) == Decimal("0.0128")
    assert metrics["net_interest_margin"]["verification_counts"].get("verified", 0) == 0
    for key, ref in expected_refs.items():
        assert metrics[key]["calculation_ref"] == ref


async def test_known_supplier_update_marks_current_industry_for_review(industry_chain):
    import json
    import shutil

    from investment_agent.api.app import create_app
    from investment_agent.cli.commands.bank_annual import import_bank_annual_snapshots

    bank_batch, _ = await import_both(industry_chain)
    changed_root = industry_chain / "supplier-update"
    shutil.copytree(FIXTURES / "bank_run02", changed_root)
    source = changed_root / "raw/financial" / f"{BANK}_main.json"
    payload = json.loads(source.read_text())
    payload["result"]["data"][0]["NONPERLOAN"] = 1.32
    payload["result"]["data"][0]["UPDATE_DATE"] = "2026-10-14 00:00:00"
    source.write_text(json.dumps(payload))
    async with session_scope() as session:
        report = await import_bank_annual_snapshots(
            session, evidence_root=changed_root, report_period=PERIOD,
            acquired_at=datetime.now(UTC),
        )
        assert report["inserted"] == 1

    client = TestClient(create_app())
    current = client.get("/industries/bank_current", headers=AUTH)
    assert current.status_code == 200, current.text
    body = current.json()
    assert body["batch_run_id"] == bank_batch
    assert body["context_status"] == "stale"
    assert body["stale_reasons"]
    assert any("复核" in warning for warning in body["warnings"])
    npl = next(row for row in body["companies"] if row["ticker"] == BANK and row["metric_key"] == "npl_ratio")
    assert Decimal(npl["value"]) == Decimal("0.0131")

    historical = client.get(f"/industries/bank_current?batch_run_id={bank_batch}", headers=AUTH)
    assert historical.status_code == 200, historical.text
    assert historical.json()["stale_reasons"] == []
    assert historical.json()["batch_run_id"] == bank_batch
