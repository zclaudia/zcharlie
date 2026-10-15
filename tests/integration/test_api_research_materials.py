"""API tests for the HK/US financial context and material refresh endpoints (plan 09).

The material used here is plan 08's checked fixture
(``tests/fixtures/materials/metrics/pilots.json``): its real amounts, real
fiscal-year spans and real source labels are stored through plan 05's
``MaterialStore`` (the single write path), so this file exercises the read model
on the same material the metric tests use — the fixture itself is read only and
never modified. The reads are asserted against the fixture's own periods: AAPL's
52/53-week fiscal years end in September, never on 31 December.

Everything runs offline against the ``api_client`` fixture's temporary database
and data directory; no test marks ``network`` and the read path is asserted to
be provably free of upstream calls (an import-graph check plus a patched HTTP
client that raises if it is ever used).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from freezegun import freeze_time
from sqlalchemy import select

from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    SubjectKind,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.contracts import (
    DatePrecision as MaterialDatePrecision,
)
from investment_agent.materials.jobs import MATERIAL_COLLECTION
from investment_agent.materials.refresh import ADAPTER_VERSIONS
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._hk_us_support import seed_pilot_listings

H = {"X-API-Key": "secret"}
FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "metrics" / "pilots.json"
)
PILOTS: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["companies"]
"""Plan 08's checked fixture amounts, read (never rewritten) by this task."""

CAPTURED_AT = datetime(2026, 10, 11, 3, 0, tzinfo=UTC)
"""Fixture acquisition instant: the day the checked identity list was verified."""

TODAY = "2026-10-12"
"""The frozen current date of the date-sensitive assertions (real dates only)."""

UNCOVERED = ("HKEX", "00777")
"""A listing that exists in the database but not in the checked identity list."""

APPLE = "apple"
HSBC = "hsbc-holdings"
APPLE_YEARS = ("FY2021", "FY2022", "FY2023", "FY2024")
HSBC_YEARS = ("FY2022", "FY2023", "FY2024")
MISSING_REASON = "发行人报告未列示该科目"

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
CONTEXT_MODULE = "investment_agent.materials.context"


def url_context(listing_id: int) -> str:
    return f"/research/listings/{listing_id}/financial-context"


def url_refresh(listing_id: int) -> str:
    return f"/research/listings/{listing_id}/refresh"


# --- seeding the local rows and the fixture material ---------------------------


async def seed_listings() -> dict[str, int]:
    """Seed the eight pilot listings plus one listing outside the list."""

    async with session_scope() as session:
        listings = await seed_pilot_listings(session)
        uncovered = await ListingRepository(session).upsert_listing(
            market_code=UNCOVERED[0],
            ticker=UNCOVERED[1],
            company_name="Uncovered Sample Limited",
        )
        seeded = {f"{market}/{ticker}": row.id for (market, ticker), row in listings.items()}
        seeded[f"{UNCOVERED[0]}/{UNCOVERED[1]}"] = uncovered.id
        return seeded


def _fixture_fact(company_key: str, entry: dict[str, Any], label: str) -> FactRecord:
    """One checked fixture fact, with its own period, unit and amount."""

    block = PILOTS[company_key]
    fiscal = next(year for year in block["fiscal_years"] if year["label"] == label)
    annual = entry["basis"] == "annual"
    return FactRecord(
        metric=entry["metric"],
        period=FactPeriod(
            start=date.fromisoformat(fiscal["start"]) if annual else None,
            end=date.fromisoformat(fiscal["end"]),
            basis=PeriodBasis.ANNUAL if annual else PeriodBasis.INSTANT,
            label=label,
        ),
        semantics=FactSemantics(
            metric=entry["metric"],
            source_label=entry["source_labels"].get(label),
            source_field=None,
            accounting_standard=AccountingStandard(block["accounting_standard"]),
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope(entry["equity_scope"]),
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=str(entry["values"][label]),
            raw_unit=entry["unit"],
            scale=entry["scale"],
            unit=entry["unit"],
            currency=block["identity"]["report_currency"],
            sign_convention=entry["sign_convention"],
        ),
        # The fixture stores no publication time, so the precision stays unknown
        # instead of a date being invented for the test.
        provenance=FactProvenance(
            source=block["source"],
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=MaterialDatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=(
            Verification.VERIFIED
            if label in entry.get("verified_years", [])
            else Verification.CANDIDATE
        ),
        conflict="none",
        applicability="applicable",
    )


def fixture_facts(company_key: str, label: str) -> tuple[FactRecord, ...]:
    block = PILOTS[company_key]
    return tuple(
        _fixture_fact(company_key, entry, label)
        for entry in block["facts"]
        if label in entry["values"]
    )


def missing_cost_fact(company_key: str, label: str) -> FactRecord:
    """One explicit missing fact (a missing amount is never 0)."""

    block = PILOTS[company_key]
    fiscal = next(year for year in block["fiscal_years"] if year["label"] == label)
    return FactRecord(
        metric="cost_of_revenue",
        period=FactPeriod(
            start=date.fromisoformat(fiscal["start"]),
            end=date.fromisoformat(fiscal["end"]),
            basis=PeriodBasis.ANNUAL,
            label=label,
        ),
        semantics=FactSemantics(
            metric="cost_of_revenue",
            source_label="营业成本",
            source_field=None,
            accounting_standard=AccountingStandard(block["accounting_standard"]),
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.UNKNOWN,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=None,
            raw_unit="USD",
            scale=1,
            unit="USD",
            currency="USD",
            sign_convention="cost of revenue as reported",
            missing_reason=MISSING_REASON,
        ),
        provenance=FactProvenance(
            source=block["source"],
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=MaterialDatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability="applicable",
    )


async def store_material(
    data_dir: Path,
    *,
    listing_id: int,
    company_key: str,
    labels: Sequence[str],
    extra: Sequence[FactRecord] = (),
) -> None:
    """Store one dataset version per fiscal year through plan 05's store."""

    block = PILOTS[company_key]
    async with session_scope() as session:
        listing = await session.get(Listing, listing_id)
        assert listing is not None
        store = MaterialStore(session, data_dir=data_dir)
        for label in labels:
            fiscal = next(year for year in block["fiscal_years"] if year["label"] == label)
            facts = (
                *fixture_facts(company_key, label),
                *(fact for fact in extra if fact.period.label == label),
            )
            await store.save(
                DatasetDraft(
                    kind=DatasetKind.ANNUAL_FACTS,
                    subject_kind=SubjectKind.COMPANY,
                    subject_id=listing.company_id,
                    source=block["source"],
                    schema_version=SCHEMA_VERSION,
                    adapter_version="fixture-1",
                    captured_at=CAPTURED_AT,
                    payload={
                        "facts": [fact_to_payload(fact) for fact in facts],
                        "pageNumber": 1,
                        "pageSize": 20,
                    },
                    period=FactPeriod(
                        start=date.fromisoformat(fiscal["start"]),
                        end=date.fromisoformat(fiscal["end"]),
                        basis=PeriodBasis.ANNUAL,
                        label=label,
                    ),
                )
            )


async def store_apple_material(data_dir: Path, listing_id: int) -> None:
    await store_material(data_dir, listing_id=listing_id, company_key=APPLE, labels=APPLE_YEARS)


async def store_hsbc_material(data_dir: Path, listing_id: int) -> None:
    await store_material(
        data_dir,
        listing_id=listing_id,
        company_key=HSBC,
        labels=HSBC_YEARS,
        extra=(missing_cost_fact(HSBC, "FY2024"),),
    )


async def pipeline_runs() -> list[dict[str, Any]]:
    """Plain rows, detached from the session (safe to assert on after commit)."""

    async with session_scope() as session:
        rows = (await session.scalars(select(PipelineRun).order_by(PipelineRun.id))).all()
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "status": row.status,
                "triggered_by": row.triggered_by,
                "listing_id": row.listing_id,
                "idempotency_key": row.idempotency_key,
                "request_json": dict(row.request_json),
                "metadata_json": dict(row.metadata_json),
            }
            for row in rows
        ]


def codes(body: dict[str, Any]) -> list[str]:
    return [status["code"] for status in body["statuses"]]


# --- GET: the read model -------------------------------------------------------


async def test_uncovered_listing_is_answered_explicitly(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.get(url_context(seeded[f"{UNCOVERED[0]}/{UNCOVERED[1]}"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["covered"] is False
    assert body["display_code"] == "00777.HK"
    assert [status["code"] for status in body["statuses"]][0] == "listing_not_covered"
    assert body["capabilities"] == {
        "financials": False,
        "price": False,
        "annual_pe": False,
        "dividends": False,
        "pb": False,
        "online_refresh": False,
    }
    assert body["facts"] == [] and body["metrics"] == [] and body["fiscal_years"] == []
    assert body["coverage"]["facts_total"] == 0
    assert body["security_type"] == "unknown"
    assert body["report_currency"] == "unknown"


async def test_covered_listing_without_material_asks_for_collection(api_client) -> None:
    seeded = await seed_listings()
    response = api_client.get(url_context(seeded["NASDAQ/AAPL"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["covered"] is True
    assert body["market"] == "NASDAQ"
    assert body["security_type"] == "ordinary"
    assert body["quote_currency"] == "USD" and body["report_currency"] == "USD"
    assert "material_missing" in codes(body)
    assert body["capabilities"]["financials"] is False
    assert body["coverage"] == {
        "periods_covered": 0,
        "facts_total": 0,
        "facts_candidate": 0,
        "facts_verified": 0,
    }


async def test_material_uses_real_fiscal_years_and_keeps_price_missing(
    api_client, tmp_path
) -> None:
    seeded = await seed_listings()
    await store_apple_material(tmp_path, seeded["NASDAQ/AAPL"])
    response = api_client.get(url_context(seeded["NASDAQ/AAPL"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"]["financials"] is True
    # A missing price capability never blocks the whole company read.
    assert body["capabilities"]["price"] is False
    assert body["facts"] and body["metrics"]
    ends = [year["end"] for year in body["fiscal_years"]]
    assert ends == ["2021-09-25", "2022-09-24", "2023-09-30", "2024-09-28"]
    assert "2024-12-31" not in ends  # no calendar year is assumed
    assert body["fiscal_years"][2]["days"] == 370  # the real 53-week year
    assert body["coverage"] == {
        "periods_covered": 4,
        "facts_total": 32,
        "facts_candidate": 32,
        "facts_verified": 0,
    }
    assert {fact["verification"] for fact in body["facts"]} == {"candidate"}
    assert "partial_verification" in codes(body)
    revenue = next(
        fact
        for fact in body["facts"]
        if fact["metric"] == "revenue_operating" and fact["period_end"] == "2024-09-28"
    )
    assert revenue["amount"] == "391035000000"
    assert revenue["currency"] == "USD"
    assert revenue["period_end"] == "2024-09-28"
    assert revenue["source"] == "发行人申报原件"
    assert revenue["source_date"] is None  # the fixture states no publication date
    assert any(metric["value"] for metric in body["metrics"])
    assert {metric["status"] for metric in body["metrics"]} & {"ok", "missing_inputs"}


async def test_bank_material_reports_not_applicable_metrics_and_missing_amounts(
    api_client, tmp_path
) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    response = api_client.get(url_context(seeded["HKEX/00005"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["company_name"] == "HSBC Holdings plc"
    assert body["display_code"] == "00005.HK"
    assert body["capabilities"]["financials"] is True
    assert body["coverage"] == {
        "periods_covered": 3,
        "facts_total": 22,
        "facts_candidate": 15,
        "facts_verified": 7,
    }
    roe = next(metric for metric in body["metrics"] if metric["key"] == "roe_avg_equity")
    assert roe["status"] == "not_applicable"
    assert any("bank" in note for note in roe["notes"])
    assert "metric_not_applicable" in codes(body)
    status = next(item for item in body["statuses"] if item["code"] == "metric_not_applicable")
    assert "平均权益 ROE" in status["affected"] and status["next_step"]
    missing = next(fact for fact in body["facts"] if fact["metric"] == "cost_of_revenue")
    assert missing["amount"] is None
    assert missing["missing_reason"] == MISSING_REASON
    assert all(fact["amount"] != "0" for fact in body["facts"])
    assert missing["source"] == "交易所年报原件"
    assert missing["locator"] is None


async def test_calendar_age_cannot_establish_latestness_without_original_review(
    api_client, tmp_path
) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    url = url_context(seeded["HKEX/00005"])
    with freeze_time("2025-06-01"):
        fresh = api_client.get(url, headers=H)
    assert fresh.status_code == 200
    assert "material_stale" not in codes(fresh.json())
    with freeze_time(TODAY):
        stale = api_client.get(url, headers=H)
    assert stale.status_code == 200
    assert "material_stale" not in codes(stale.json())
    assert "latestness_unknown" in codes(stale.json())
    assert stale.json()["freshness"]["last_verified_on"] is None
    assert stale.json()["freshness"]["latestness"] == "unknown"


async def test_capability_bits_follow_the_online_refresh_flag(api_client, monkeypatch) -> None:
    from investment_agent.materials import handlers

    seeded = await seed_listings()
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    with freeze_time(TODAY):
        response = api_client.get(url_context(seeded["NASDAQ/AAPL"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    assert body["capabilities"]["online_refresh"] is True
    assert "online_refresh_disabled" not in codes(body)
    assert body["capabilities"]["pb"] is False  # still not delivered


async def test_unresolved_source_conflict_is_visible_and_not_resolved(api_client, tmp_path) -> None:
    """A conflicting source value is shown as such and never averaged or picked."""

    seeded = await seed_listings()
    base = next(
        fact for fact in fixture_facts(APPLE, "FY2024") if fact.metric == "revenue_operating"
    )
    conflict = replace(
        base,
        value=replace(base.value, raw_value="380000000000"),
        conflict="unresolved",
        conflict_note="两个来源的收入金额不同，未判定",
    )
    await store_material(
        tmp_path,
        listing_id=seeded["NASDAQ/AAPL"],
        company_key=APPLE,
        labels=APPLE_YEARS,
        extra=(conflict,),
    )
    with freeze_time(TODAY):
        response = api_client.get(url_context(seeded["NASDAQ/AAPL"]), headers=H)
    assert response.status_code == 200
    body = response.json()
    unresolved = [
        fact
        for fact in body["facts"]
        if fact["metric"] == "revenue_operating" and fact["conflict"] == "unresolved"
    ]
    assert len(unresolved) == 1
    status = next(item for item in body["statuses"] if item["code"] == "source_conflict")
    assert "收入" in status["affected"][0]
    revenue_yoy = next(metric for metric in body["metrics"] if metric["key"] == "revenue_yoy")
    assert revenue_yoy["status"] == "missing_inputs"
    assert any("conflicting facts" in note for note in revenue_yoy["notes"])


async def test_get_financial_context_is_read_only_and_never_calls_upstream(
    api_client, tmp_path, monkeypatch
) -> None:
    seeded = await seed_listings()
    await store_apple_material(tmp_path, seeded["NASDAQ/AAPL"])
    calls: list[str] = []

    async def forbidden(*args: object, **kwargs: object) -> httpx.Response:
        calls.append("httpx.AsyncClient.request")
        raise RuntimeError("the read path must not call upstream")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    before = await pipeline_runs()
    url = url_context(seeded["NASDAQ/AAPL"])
    with freeze_time(TODAY):
        first, second = api_client.get(url, headers=H), api_client.get(url, headers=H)
    assert first.status_code == 200 and second.status_code == 200
    assert calls == []
    assert await pipeline_runs() == before == []


async def test_get_response_carries_no_upstream_identifiers(api_client, tmp_path) -> None:
    seeded = await seed_listings()
    await store_hsbc_material(tmp_path, seeded["HKEX/00005"])
    with freeze_time(TODAY):
        response = api_client.get(url_context(seeded["HKEX/00005"]), headers=H)
    assert response.status_code == 200
    text = response.text
    for forbidden in ("CIK", "eastmoney", "sec.gov", "accession"):
        assert forbidden.lower() not in text.lower()
    assert "HSBC" in text  # the real company name is shown


async def test_get_unknown_listing_is_404(api_client) -> None:
    await seed_listings()
    assert api_client.get(url_context(999_999), headers=H).status_code == 404


def test_context_import_graph_has_no_fetcher_and_no_http_client() -> None:
    graph = _import_graph(CONTEXT_MODULE)
    assert "investment_agent.materials.hk_native" not in graph
    assert "investment_agent.materials.us_sec" not in graph
    assert "investment_agent.materials.offline_import" not in graph
    assert "httpx" not in graph
    # The walker really follows the read path (otherwise the assertions are empty).
    assert "investment_agent.materials.evidence" in graph
    assert "investment_agent.materials.metrics" in graph
    assert "investment_agent.storage.repositories.research_material" in graph


# --- POST: the licensed refresh entry ------------------------------------------


async def test_refresh_deduplicates_an_identical_request(api_client) -> None:
    seeded = await seed_listings()
    url = url_refresh(seeded["NASDAQ/AAPL"])
    body = {"sources": ["sec.companyfacts.v1"]}
    with freeze_time(TODAY):
        first = api_client.post(url, headers=H, json=body)
        second = api_client.post(url, headers=H, json=body)
    assert first.status_code == 202 and second.status_code == 202
    created, repeated = first.json(), second.json()
    assert created["created"] is True and repeated["created"] is False
    assert created["run_id"] == repeated["run_id"]
    assert created["idempotency_key"] == repeated["idempotency_key"]
    assert created["idempotency_key"].startswith("material_collection:")
    assert created["adapter_versions"] == {"sec.companyfacts.v1": "1"}
    rows = await pipeline_runs()
    assert len(rows) == 1
    run = rows[0]
    assert run["kind"] == MATERIAL_COLLECTION
    assert run["status"] == "queued"
    assert run["triggered_by"] == "api:refresh"
    assert run["listing_id"] == seeded["NASDAQ/AAPL"]
    assert run["request_json"]["refresh_batch"] == "sec.companyfacts.v1|2026-10-12"
    assert run["request_json"]["adapter_versions"] == {"sec.companyfacts.v1": "1"}
    assert run["request_json"]["periods"] == []


async def test_refresh_binds_explicit_periods_without_dropping_them(api_client) -> None:
    seeded = await seed_listings()
    url = url_refresh(seeded["NASDAQ/AAPL"])
    with freeze_time(TODAY):
        first = api_client.post(
            url, headers=H, json={"sources": ["sec.companyfacts.v1"], "periods": ["2024-09-28"]}
        )
        repeat = api_client.post(
            url, headers=H, json={"sources": ["sec.companyfacts.v1"], "periods": ["2024-09-28"]}
        )
        other = api_client.post(
            url, headers=H, json={"sources": ["sec.companyfacts.v1"], "periods": ["2023-09-30"]}
        )
    assert [response.status_code for response in (first, repeat, other)] == [202, 202, 202]
    assert first.json()["created"] is True
    assert repeat.json()["created"] is False
    assert repeat.json()["idempotency_key"] == first.json()["idempotency_key"]
    # A different period set is a different request: nothing was silently dropped.
    assert other.json()["created"] is True
    assert other.json()["idempotency_key"] != first.json()["idempotency_key"]
    rows = await pipeline_runs()
    assert len(rows) == 2
    assert [row["request_json"]["periods"] for row in rows] == [["2024-09-28"], ["2023-09-30"]]
    assert {row["request_json"]["refresh_batch"] for row in rows} == {
        "sec.companyfacts.v1|2026-10-12"
    }


async def test_refresh_refuses_bad_requests_without_writing_a_run(api_client) -> None:
    seeded = await seed_listings()
    cases = [
        (
            999_999,
            {},
            404,
            None,
        ),
        (
            seeded[f"{UNCOVERED[0]}/{UNCOVERED[1]}"],
            {},
            422,
            "listing_not_covered",
        ),
        (
            seeded["NASDAQ/AAPL"],
            {"sources": ["not.a.source"]},
            422,
            "unknown_source",
        ),
        (
            seeded["NASDAQ/AAPL"],
            {"periods": ["2024-13-01"]},
            422,
            "invalid_period",
        ),
    ]
    for listing_id, body, status, code in cases:
        response = api_client.post(url_refresh(listing_id), headers=H, json=body)
        assert response.status_code == status, (listing_id, body, response.text)
        if code is not None:
            assert response.json()["detail"]["code"] == code
    assert await pipeline_runs() == []


async def test_refresh_reports_the_disabled_online_refresh(api_client) -> None:
    seeded = await seed_listings()
    url = url_refresh(seeded["NASDAQ/AAPL"])
    response = api_client.post(url, headers=H, json={})
    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    assert body["online_refresh_enabled"] is False
    assert "已采集" not in response.text
    rows = await pipeline_runs()
    assert len(rows) == 1
    # The run is only queued; its metadata claims no check and no fetch.
    assert rows[0]["status"] == "queued"
    assert rows[0]["metadata_json"] == {}
    assert "已采集" not in json.dumps(rows[0]["metadata_json"])
    assert rows[0]["request_json"]["sources"] == [
        "eastmoney.hk.native.v1",
        "sec.companyfacts.v1",
    ]
    # Queuing a run never flips the capability bit.
    context = api_client.get(url_context(seeded["NASDAQ/AAPL"]), headers=H).json()
    assert context["capabilities"]["online_refresh"] is False
    assert "online_refresh_disabled" in codes(context)


def test_refresh_adapter_versions_cover_every_material_source() -> None:
    from investment_agent.materials.jobs import MATERIAL_SOURCES

    assert set(ADAPTER_VERSIONS) == set(MATERIAL_SOURCES)
    assert all(version for version in ADAPTER_VERSIONS.values())


# --- authentication ------------------------------------------------------------


def test_financial_context_requires_authentication(api_client) -> None:
    assert api_client.get(url_context(1)).status_code == 401
    assert api_client.get(url_context(1), headers={"X-API-Key": "nope"}).status_code == 401


def test_refresh_requires_authentication(api_client) -> None:
    assert api_client.post(url_refresh(1), json={}).status_code == 401
    assert (
        api_client.post(url_refresh(1), headers={"X-API-Key": "nope"}, json={}).status_code == 401
    )


# --- the import-graph walker ---------------------------------------------------


def _module_level_imports(name: str, path: Path) -> set[str]:
    """Every module named by the module-level import statements of ``path``."""

    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                module = f"{base}.{node.module}" if node.module else base
            elif node.module:
                module = node.module
            else:
                continue
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _module_path(name: str) -> Path | None:
    relative = Path(*name.split("."))
    for candidate in (SRC_ROOT / relative.with_suffix(".py"), SRC_ROOT / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _import_graph(entry: str) -> set[str]:
    """The module-level import graph of ``entry`` (package-local recursion).

    Function-local imports are deliberately not followed: a local import is a
    laziness boundary chosen by its module, not a dependency of the module's
    own graph. The runtime side of that boundary is covered by the patched HTTP
    client test above.
    """

    seen: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_path(name)
        if path is not None:
            pending.extend(_module_level_imports(name, path))
    return seen
