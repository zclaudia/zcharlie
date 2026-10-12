"""Public offline CLI: fresh setup, real adapters, shards and visible statements."""

import asyncio
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from investment_agent.cli.main import app
from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.context import financial_context
from investment_agent.materials.contracts import RawPayload
from investment_agent.materials.hk_native import HkNativeAdapter
from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.research_material import ResearchDatasetRepository
from tests.integration.test_migration_046 import _config
from tests.unit.test_material_hk_native import HSBC_2024

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "materials"


def _invoke(*args: str):
    return CliRunner().invoke(app, ["materials", *args])


def test_fresh_migrated_database_initialization_is_repeatable(tmp_path, monkeypatch):
    from alembic import command

    db = tmp_path / "fresh.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_KEY", "secret")
    command.upgrade(_config(db, monkeypatch), "head")
    first = _invoke("init")
    assert first.exit_code == 0, first.output
    setup = json.loads(first.output)
    assert len(setup["listings"]) == len(setup["company_ids"]) == 8
    second = _invoke("init")
    assert second.exit_code == 0, second.output
    repeated = json.loads(second.output)
    assert repeated["bindings_created"] == 0
    assert repeated["listings"] == setup["listings"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM listing").fetchone() == (8,)
        assert conn.execute("SELECT count(*) FROM company").fetchone() == (8,)
        assert conn.execute("SELECT count(*) FROM market").fetchone() == (3,)
        assert conn.execute("SELECT count(*) FROM research_source_binding").fetchone() == (
            setup["bindings_created"],
        )


async def test_real_import_reaches_api_and_page_and_reads_sec_shards(api_client, tmp_path, monkeypatch):
    async def forbid_network(*args, **kwargs):
        raise AssertionError("offline material commands must never access the network")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbid_network)
    evidence = tmp_path / "evidence"
    shutil.copytree(FIXTURES / "bootstrap", evidence)
    # An older single-year alias must not take precedence over the complete corpus.
    shutil.copyfile(FIXTURES / "hk" / "00005" / "income.json", evidence / "00005" / "income.json")
    shutil.copytree(FIXTURES / "us" / "JPM", evidence / "JPM")
    for path in (evidence / "JPM").rglob("*.json"):
        body = path.read_bytes()
        path.with_name(path.name + ".meta.json").write_text(json.dumps({
            "status": 200, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            # Test acquisition time; publication/acceptance comes from SEC bodies.
            "fetched_at_utc": "2026-10-12T00:00:00+00:00",
        }))
    args = ("import", "--source-dir", str(evidence), "--company-key", "hsbc-holdings",
            "--company-key", "jpmorgan-chase")
    first = await asyncio.to_thread(_invoke, *args)
    # HSBC also binds a SEC cross-check, absent from this deliberately partial corpus.
    assert first.exit_code == 2, first.output
    report = json.loads(first.output)
    assert report["datasets_created"] == 6
    assert report["skipped"] == ["hsbc-holdings/sec.companyfacts.v1"]
    assert not any("jpmorgan-chase/" in item for item in report["problems"])
    listing_id = report["listings"]["HKEX/00005"]
    response = api_client.get(f"/research/listings/{listing_id}/financial-context",
                              headers={"X-API-Key": "secret"})
    assert response.status_code == 200, response.text
    context = response.json()
    assert context["report_currency"] == "USD"
    assert context["quote_currency"] == "HKD"
    latest = {fact["metric"]: fact["amount"] for fact in context["facts"]
              if fact["period_end"] == "2024-12-31"}
    for metric, amount in HSBC_2024.items():
        assert latest[metric] == amount
    assert len(context["fiscal_years"]) == 4
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    page = api_client.get("/app/stocks/00005.HK")
    assert page.status_code == 200
    for metric in ("revenue_net_credit", "total_assets", "operating_cash_flow"):
        assert f'data-metric="{metric}"' in page.text
    async with session_scope() as session:
        jpm_id = report["company_ids"]["jpmorgan-chase"]
        indexes = await ResearchDatasetRepository(session).history(
            kind="filing_index", subject_kind="company", subject_id=jpm_id,
        )
        assert len(indexes) == 1
        assert set(indexes[0].payload["shards"]) == {
            "CIK0000019617-submissions-007.json", "CIK0000019617-submissions-035.json",
        }
        assert indexes[0].payload["shards_missing"] == []
    second = await asyncio.to_thread(_invoke, *args)
    assert second.exit_code == 2, second.output
    repeated = json.loads(second.output)
    assert repeated["datasets_created"] == 0
    assert repeated["datasets_reused"] == report["datasets_created"]


async def test_setup_refuses_changed_binding_without_partial_writes(db_session):
    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    setup = await initialize_pilot(db_session, identities)
    await db_session.commit()
    original = identities[0]
    changed = replace(original, valid_from=original.valid_from + timedelta(days=1))
    new = replace(original, ticker="00003", company_key="test-new-company",
                  company_name="Test New Company", company_name_zh="测试公司",
                  sources=(replace(original.sources[0], symbol="00003.HK"),
                           replace(original.sources[1], symbol="0003.HK")))
    with pytest.raises(ValueError, match="refusing to rewrite"):
        await initialize_pilot(db_session, (new, changed, *identities[1:]))
    repeated = await initialize_pilot(db_session, identities)
    assert repeated.bindings_created == 0
    assert repeated.company_ids == setup.company_ids
    from sqlalchemy import func, select

    from investment_agent.storage.models.reference import Listing

    assert await db_session.scalar(select(func.count()).select_from(Listing)) == 8


async def test_legacy_statement_versions_remain_visible_until_consolidated(db_session, tmp_path):
    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    identity = next(row for row in identities if row.company_key == "hsbc-holdings")
    setup = await initialize_pilot(db_session, identities)
    company_id = setup.company_ids[identity.company_key]
    names = {"income": "income-four-years.json", "balance": "balance-four-years.json",
             "cashflow": "cashflow-original-four-years.json"}
    evidence = FIXTURES / "bootstrap"
    raw = tuple(RawPayload(
        name=name, content=(evidence / "00005" / filename).read_bytes(),
        media_type="application/json", fetched_at=datetime.now(UTC),
        source_url="https://example.invalid/fixture", http_status=200,
    ) for name, filename in names.items())
    drafts = HkNativeAdapter().parse(identity, raw=raw, periods=(), subject_id=company_id).drafts
    store = MaterialStore(db_session, data_dir=tmp_path)
    for draft in drafts:
        await store.save(draft)  # Reproduce the old 12-row layout.
    listing_id = setup.listings[("HKEX", "00005")].id
    old = await financial_context(db_session, listing_id)
    await import_offline_evidence(
        db_session, data_dir=tmp_path, source_dir=evidence, identities=(identity,),
        company_ids=setup.company_ids, adapters={HkNativeAdapter().source: HkNativeAdapter()},
    )
    consolidated = await financial_context(db_session, listing_id)
    assert len(old.facts) == len(consolidated.facts)  # No dropped tables or doubled facts.
    for view in (old, consolidated):
        latest = {fact.metric: fact.amount for fact in view.facts
                  if fact.period_end.isoformat() == "2024-12-31"}
        for metric, amount in HSBC_2024.items():
            assert latest[metric] == amount


@pytest.mark.parametrize("market", ["hk", "us"])
@pytest.mark.parametrize("status", [301, 403, 429, 500])
def test_real_adapters_reject_http_failure_even_with_valid_financial_body(market, status):
    from investment_agent.materials.us_sec import SecFactsAdapter

    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    company_key = "hsbc-holdings" if market == "hk" else "jpmorgan-chase"
    identity = next(row for row in identities if row.company_key == company_key)
    if market == "hk":
        path = FIXTURES / "bootstrap" / "00005" / "income-four-years.json"
        adapter = HkNativeAdapter()
        name = "income"
    else:
        path = FIXTURES / "us" / "JPM" / "companyfacts.json"
        adapter = SecFactsAdapter()
        name = "companyfacts.json"
    raw = RawPayload(name=name, content=path.read_bytes(), media_type="application/json",
                     fetched_at=datetime.now(UTC), source_url="https://example.invalid/fixture",
                     http_status=status)
    result = adapter.parse(identity, raw=(raw,), periods=(), subject_id=1)
    assert result.drafts == ()
    assert result.problems == (f"http_status_rejected: {name}/{status}",)
