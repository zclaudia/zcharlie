#!/usr/bin/env python3
"""Run the six HK2-06 operational drills as a socket-audited offline simulation.

Example (from the repository root)::

    PYTHONPATH=src python tools/hk206_drills.py \
        --source eastmoney.hk.native.v1 --evidence-root /new/empty/dir

Every drill drives the real service chain (queue, worker, handler, adapters,
archiver, store, isolated probe) with only the HTTP transport replaced by
local mock responses, and a process-wide audit hook aborts the process on any
real ``socket.connect`` attempt. The command never loads ``.env`` and ignores
``DATABASE_URL`` / ``DATA_DIR`` from the environment: each drill builds its
own SQLite database and data directory under ``<evidence-root>/scratch/`` and
passes them explicitly through ``run_with_engine``. There is no network
escape hatch: this tool cannot perform a real run, every record it writes is
``mode=simulation`` with ``network_calls=0``, and it never flips the product
default of ``ONLINE_REFRESH_ENABLED=False`` (in-process flips are restored
and asserted before any evidence is written).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SEC_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "materials" / "us"
# Synthetic fixture mapping copied from tests/integration/
# test_hk206_sec_probe_transport.py (HSBC is the TSM sample relabelled with
# HSBC's CIK; every byte stays explicitly synthetic).
SEC_FIXTURE_BY_COMPANY: dict[str, str] = {
    "apple": "AAPL",
    "costco": "COST",
    "jpmorgan-chase": "JPM",
    "exxon-mobil": "XOM",
    "tsmc": "TSM",
    "hsbc-holdings": "TSM",
}
DRILL_ORDER: tuple[str, ...] = (
    "shutdown",
    "inflight_stop",
    "restart_resume",
    "old_versions_readable",
    "offline_replay",
    "probe_stop_withdrawal",
)
DRILL_YEARS = range(2021, 2025)
USER_AGENT = "ZCharlie HK2-06 drill simulation (contact: drills@zcharlie.org)"
SYNTHETIC_CITATION = "synthetic:hk206-drill/licensing"
OLD_ADAPTER_VERSION = "0"
DRILL_MIN_INTERVAL = 0.01
HK_SOURCE = "eastmoney.hk.native.v1"
SEC_SOURCE = "sec.companyfacts.v1"

_NETWORK_BLOCKED = 0


class DrillExecutionError(RuntimeError):
    """The drill could not exercise the service chain as designed."""


def _install_network_audit_hook() -> None:
    """Abort the process if anything tries to open a real socket."""

    def guard(event: str, args: tuple[Any, ...]) -> None:
        global _NETWORK_BLOCKED
        if event in ("socket.connect", "socket.connect_ex", "socket.getaddrinfo"):
            _NETWORK_BLOCKED += 1
            raise RuntimeError(
                f"hk206 drill simulation attempted real network access ({event})"
            )

    sys.addaudithook(guard)


# --- shared plumbing ---------------------------------------------------------


@dataclass
class DrillWorkspace:
    """One drill's isolated evidence directory and scratch environment."""

    source: str
    drill: str
    slug: str
    evidence_root: Path
    evidence_dir: Path
    data_dir: Path
    database_url: str

    def relpath(self, path: Path) -> str:
        return path.relative_to(self.evidence_root).as_posix()


def _new_workspace(
    evidence_root: Path, source: str, drill: str, slug: str | None = None
) -> DrillWorkspace:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{slug or drill}-{stamp}"
    evidence_dir = evidence_root / "drills" / source / name
    scratch_dir = evidence_root / "scratch" / source / name
    if evidence_dir.exists() or evidence_dir.is_symlink():
        raise DrillExecutionError(f"drill evidence directory already exists: {evidence_dir}")
    evidence_dir.mkdir(parents=True)
    data_dir = scratch_dir / "data"
    data_dir.mkdir(parents=True)
    return DrillWorkspace(
        source=source,
        drill=drill,
        slug=slug or drill,
        evidence_root=evidence_root,
        evidence_dir=evidence_dir,
        data_dir=data_dir,
        database_url=f"sqlite+aiosqlite:///{(scratch_dir / 'drill.sqlite3').as_posix()}",
    )


@dataclass
class DrillLedger:
    """The steps, requests and failures one drill observed."""

    steps: list[dict[str, object]] = field(default_factory=list)
    requests: list[dict[str, object]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def step(self, action: str, expected: str, observed: str, *, passed: bool) -> None:
        self.steps.append(
            {"action": action, "expected": expected, "observed": observed, "passed": passed}
        )
        if not passed:
            self.failures.append(f"{action}: expected {expected}; observed {observed}")


class MockWire:
    """The drill's fake upstream wire: catalog answers, scripted 500s, hooks."""

    def __init__(self, catalog: Mapping[str, tuple[str, str, bytes]]) -> None:
        self.catalog = catalog
        self.served: list[tuple[str, str, int]] = []
        self.fail_at_occurrence: dict[tuple[str, str], int] = {}
        self.on_served: Callable[[str, str], None] | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        entry = self.catalog.get(key)
        if entry is None:
            raise DrillExecutionError(f"unexpected drill request: {key}")
        company_key, name, body = entry
        occurrence = 1 + sum(
            1 for served_company, served_name, _ in self.served
            if (served_company, served_name) == (company_key, name)
        )
        status = 500 if self.fail_at_occurrence.get((company_key, name)) == occurrence else 200
        self.served.append((company_key, name, status))
        if status == 200 and self.on_served is not None:
            self.on_served(company_key, name)
        return httpx.Response(
            status, content=body, headers={"content-type": "application/json"}
        )

    @property
    def request_ledger(self) -> list[dict[str, object]]:
        return [
            {"company_key": company, "name": name, "http_status": status, "transport": "mock"}
            for company, name, status in self.served
        ]

    def count(self, company: str, name: str) -> int:
        return sum(
            1 for served_company, served_name, _ in self.served
            if (served_company, served_name) == (company, name)
        )

    def count_company(self, company: str) -> int:
        return sum(1 for served_company, _, _ in self.served if served_company == company)


def _hk_annual_rows(identity: Any, name: str) -> list[dict[str, object]]:
    """Synthetic HK rows for one table (construction copied from
    tests/unit/test_material_source_probe.py so the real HkNativeAdapter parses
    them; nothing here is real market data)."""

    rows: list[dict[str, object]] = []
    for year in DRILL_YEARS:
        end = f"{year}-12-31"
        common: dict[str, object] = {
            "STD_REPORT_DATE": end,
            "START_DATE": f"{year}-01-01",
            "CURRENCY_CODE": identity.report_currency,
            "CURRENCY_ABBR": identity.report_currency,
            "ACCOUNT_STANDARD": "国际会计准则",
            "REPORT_TYPE": "年报",
            "FISCAL_YEAR": str(year),
        }
        if name == "income":
            rows.append({**common, "STD_ITEM_CODE": "004001999", "ITEM_NAME": "营运收入",
                         "AMOUNT": year * 100})
        elif name == "balance":
            rows.append({**common, "STD_ITEM_CODE": "004009999", "ITEM_NAME": "资产总计",
                         "AMOUNT": year * 1000})
        else:
            rows.append({
                "REPORT_DATE": end,
                "VALUE_LIST": [{**common, "STD_ITEM_CODE": "003999",
                                "ITEM_NAME": "经营活动现金流量净额", "AMOUNT": year * 10}],
            })
    return rows


def _hk_body(identity: Any, request: Any) -> bytes:
    data = _hk_annual_rows(identity, request.name)
    body = {
        "success": True,
        "version": "synthetic-drill-response-1",
        "result": {"data": data, "count": len(data), "pages": 1},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _hk_drill_periods() -> tuple[Any, ...]:
    from investment_agent.materials.contracts import FactPeriod, PeriodBasis

    return tuple(
        FactPeriod(start=date(year, 1, 1), end=date(year, 12, 31), basis=PeriodBasis.ANNUAL)
        for year in DRILL_YEARS
    )


def _sec_documents(company_key: str, cik: int) -> dict[str, bytes]:
    """Load the synthetic SEC fixture documents for one company (copied from
    tests/integration/test_hk206_sec_probe_transport.py)."""

    folder = SEC_FIXTURE_ROOT / SEC_FIXTURE_BY_COMPANY[company_key]
    documents: dict[str, bytes] = {}
    for name in ("companyfacts.json", "submissions.json"):
        value = json.loads((folder / name).read_text(encoding="utf-8"))
        value["cik"] = cik
        documents[name] = json.dumps(value, ensure_ascii=False).encode("utf-8")
    submissions = json.loads(documents["submissions.json"])
    for shard in submissions.get("filings", {}).get("files", []):
        documents[shard["name"]] = (folder / "shards" / shard["name"]).read_bytes()
    return documents


def _build_catalog(source: str, identities: Sequence[Any]) -> dict[str, tuple[str, str, bytes]]:
    """Every URL the drill may serve, mapped to company/request/body."""

    catalog: dict[str, tuple[str, str, bytes]] = {}
    if source == HK_SOURCE:
        from investment_agent.materials.hk_native import HkNativeAdapter

        adapter = HkNativeAdapter()
        for identity in identities:
            for request in adapter.requests(identity, periods=_hk_drill_periods()):
                url = str(httpx.URL(request.url, params=list(request.params)))
                catalog[url] = (identity.company_key, request.name, _hk_body(identity, request))
        return catalog
    from investment_agent.materials.us_sec import SHARD_URL, SecFactsAdapter

    adapter = SecFactsAdapter()
    for identity in identities:
        documents = _sec_documents(
            identity.company_key,
            int(next(
                binding.symbol for binding in identity.sources if binding.source == source
            )),
        )
        for request in adapter.requests(identity):
            url = str(httpx.URL(request.url, params=list(request.params)))
            catalog[url] = (identity.company_key, request.name, documents[request.name])
        for name, body in documents.items():
            if name not in ("companyfacts.json", "submissions.json"):
                catalog[str(httpx.URL(SHARD_URL.format(name=name)))] = (
                    identity.company_key,
                    name,
                    body,
                )
    return catalog


def _drill_settings(data_dir: Path, *, operational: bool) -> Any:
    from investment_agent.config import Settings

    return Settings.model_construct(
        api_key="hk206-drill-offline-simulation-key-000000000000",
        llm_tier3_api_key="offline-drill-unused",
        data_dir=data_dir,
        materials_sec_refresh_enabled=operational,
        materials_hk_refresh_enabled=operational,
    )


def _drill_approvals(source: str, identities: Sequence[Any], adapter_version: str) -> dict[str, Any]:
    keys = [identity.company_key for identity in identities]
    return {
        source: {
            "licensing": {
                "status": "approved",
                "company_keys": keys,
                "citation": SYNTHETIC_CITATION,
            },
            "technical_acceptance": {
                "status": "accepted",
                "company_keys": keys,
                "adapter_version": adapter_version,
                "reports": [
                    {
                        "observed_on": f"2026-10-{day:02d}",
                        "citation": "synthetic:hk206-drill/report",
                    }
                    for day in (14, 15, 16)
                ],
            },
        }
    }


@contextlib.contextmanager
def _global_gate_open() -> Any:
    """Flip the worker's module gate in this drill process, restoring False."""

    from investment_agent.materials import handlers

    if handlers.ONLINE_REFRESH_ENABLED is not False:
        raise DrillExecutionError("product ONLINE_REFRESH_ENABLED must default to False")
    handlers.ONLINE_REFRESH_ENABLED = True
    try:
        yield
    finally:
        handlers.ONLINE_REFRESH_ENABLED = False


@contextlib.contextmanager
def _flippable_source_switch(source: str) -> Any:
    """Let the drill close the source's operational settings switch mid-run.

    The handler re-resolves ``load_settings`` before every request, so patching
    the loader is the faithful in-process way to simulate an operator closing
    the source refresh switch while a run is in flight.
    """

    from investment_agent.settings import loader

    original = loader.load_settings
    state: dict[str, bool] = {"closed": False}
    update_key = (
        "materials_sec_refresh_enabled" if source == SEC_SOURCE else "materials_hk_refresh_enabled"
    )

    async def patched(session: Any = None, *, base: Any = None) -> Any:
        effective = await original(session, base=base)
        if state["closed"]:
            return effective.model_copy(update={update_key: False})
        return effective

    loader.load_settings = patched
    try:
        yield state
    finally:
        loader.load_settings = original


async def _prepare_database(pilot_list_path: Path) -> tuple[tuple[Any, ...], dict[Any, Any]]:
    from investment_agent.domain.security import MARKETS, load_pilot_identities
    from investment_agent.storage.database import get_engine, session_scope
    from investment_agent.storage.models import Base
    from investment_agent.storage.repositories.listing import ListingRepository

    engine = get_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    identities = load_pilot_identities(pilot_list_path)
    async with session_scope() as session:
        repository = ListingRepository(session)
        for market in MARKETS.values():
            await repository.ensure_market(market)
        listings = await repository.ensure_controlled_listings(identities)
        await session.commit()
    return identities, listings


async def _enqueue_run(
    listing_id: int,
    *,
    sources: Sequence[str],
    adapter_versions: Mapping[str, str],
    batch: str,
) -> int:
    from investment_agent.materials.jobs import MaterialCollectionPayload, enqueue_material_job
    from investment_agent.storage.database import session_scope

    payload = MaterialCollectionPayload(
        listing_id=listing_id,
        sources=tuple(sources),
        refresh_batch=batch[:64],
        adapter_versions=dict(adapter_versions),
        reason="hk206_drill_simulation",
    )
    async with session_scope() as session:
        run, _created = await enqueue_material_job(session, payload, triggered_by="hk206-drills")
        return run.id


async def _run_row(run_id: int) -> dict[str, Any]:
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        if run is None:
            raise DrillExecutionError(f"drill run {run_id} disappeared")
        return {
            "id": run.id,
            "status": run.status,
            "attempt_count": run.attempt_count,
            "metadata": run.metadata_json if isinstance(run.metadata_json, dict) else {},
        }


def _make_worker(
    settings: Any, fetcher: Any, adapters: Mapping[str, Any], approvals: Mapping[str, Any]
) -> Any:
    from investment_agent.materials.handlers import MaterialCollectionHandler
    from investment_agent.research.worker import ResearchWorker

    handler = MaterialCollectionHandler(
        settings=settings, fetcher=fetcher, adapters=adapters, approvals=approvals
    )
    return ResearchWorker(
        settings=settings,
        worker_id="hk206-drill",
        handlers=[handler],
        retry_base_delay_seconds=0.0,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_artifact(
    ws: DrillWorkspace, filename: str, kind: str, payload: object
) -> dict[str, object]:
    target = ws.evidence_dir / filename
    if target.exists():
        raise DrillExecutionError(f"drill artifact already exists: {target}")
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {"kind": kind, "path": filename, "sha256": _sha256_file(target)}


def _finalize_evidence(
    ws: DrillWorkspace,
    ledger: DrillLedger,
    artifacts: Sequence[Mapping[str, object]],
    *,
    service_chain: Sequence[str],
    adapter_version: str,
    stop_semantics: Any = None,
) -> dict[str, object]:
    from investment_agent.materials.drill_evidence import (
        DrillArtifact,
        DrillEvidence,
        DrillRequest,
        DrillStep,
        load_drill_evidence,
        write_drill_evidence,
    )
    from investment_agent.materials.source_observations import SOURCE_COMPANIES

    passed = not ledger.failures and all(bool(step["passed"]) for step in ledger.steps)
    evidence = DrillEvidence(
        schema_version=1,
        drill=ws.drill,
        mode="simulation",
        source=ws.source,
        adapter_version=adapter_version,
        executed_at=datetime.now(UTC),
        isolated_environment={"database_uri": ws.database_url, "data_dir": str(ws.data_dir)},
        service_chain=tuple(service_chain),
        company_scope=SOURCE_COMPANIES[ws.source],
        steps=tuple(DrillStep(**step) for step in ledger.steps),  # type: ignore[arg-type]
        requests=tuple(
            DrillRequest(
                company_key=str(item["company_key"]),
                name=str(item["name"]),
                http_status=int(item["http_status"]),  # type: ignore[arg-type]
                transport=str(item["transport"]),
            )
            for item in ledger.requests
        ),
        artifacts=tuple(
            DrillArtifact(
                kind=str(item["kind"]), path=str(item["path"]), sha256=str(item["sha256"])
            )
            for item in artifacts
        ),
        network_calls=0,
        passed=passed,
        failure_reasons=tuple(ledger.failures),
        stop_semantics=stop_semantics,
    )
    path = write_drill_evidence(ws.evidence_dir, evidence)
    reloaded = load_drill_evidence(path)
    return {
        "passed": reloaded.passed,
        "evidence_path": ws.relpath(path),
        "sha256": _sha256_file(path),
        "failures": list(ledger.failures),
    }


def _verify_archive(data_dir: Path, manifest_ref: str, manifest_sha256: str) -> Any:
    from investment_agent.materials.archive import verify_capture

    return verify_capture(Path(data_dir), manifest_ref, expected_sha256=manifest_sha256)


async def _dataset_rows() -> list[Any]:
    from sqlalchemy import select

    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.research_material import ResearchDataset

    async with session_scope() as session:
        return list((await session.execute(select(ResearchDataset))).scalars())


# --- drill 1: shutdown -------------------------------------------------------


def _run_shutdown(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.storage.database import run_with_engine

    return run_with_engine(ws.database_url, lambda: _shutdown_async(ws, pilot_list_path))


async def _shutdown_async(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.domain.security import source_symbol
    from investment_agent.materials import handlers
    from investment_agent.materials.drill_evidence import DrillStopSemantics
    from investment_agent.materials.handlers import HttpxMaterialFetcher
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.us_sec import SecFactsAdapter

    ledger = DrillLedger()
    identities, listings = await _prepare_database(pilot_list_path)
    bound = [
        identity for identity in identities if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    adapter = SecFactsAdapter() if ws.source == SEC_SOURCE else HkNativeAdapter()
    approvals = _drill_approvals(ws.source, bound, adapter.adapter_version)
    wire = MockWire(_build_catalog(ws.source, bound))
    records: list[dict[str, Any]] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire.respond))
    try:
        # Phase A: the operational switch is closed before the run starts.
        fetcher = HttpxMaterialFetcher(
            client=client, user_agent=USER_AGENT, min_interval={ws.source: DRILL_MIN_INTERVAL}
        )
        settings = _drill_settings(ws.data_dir, operational=False)
        worker = _make_worker(settings, fetcher, {ws.source: adapter}, approvals)
        run_ids = [
            await _enqueue_run(
                listings[(identity.market_code, identity.ticker)].id,
                sources=(ws.source,),
                adapter_versions={ws.source: adapter.adapter_version},
                batch=f"shutdown-a-{identity.company_key}",
            )
            for identity in bound
        ]
        for run_id in run_ids:
            await worker.run_once(run_id=run_id)
        phase_a_rows = [await _run_row(run_id) for run_id in run_ids]
        decisions_disabled = all(
            row["metadata"]
            .get("source_decisions", {})
            .get(ws.source, {})
            .get("enabled") is False
            for row in phase_a_rows
        )
        ledger.step(
            "run the worker with the operational source switch already closed",
            "no fetch starts, every decision records not enabled, no run reports success",
            (
                f"{len(phase_a_rows)} runs closed as "
                f"{sorted(row['status'] for row in phase_a_rows)}; "
                f"requests issued: {len(wire.served)}; decisions not enabled: "
                f"{decisions_disabled}"
            ),
            passed=(
                all(row["status"] == "partial" for row in phase_a_rows)
                and not wire.served
                and decisions_disabled
            ),
        )

        # Phase B: the switch closes after the first response of a live run.
        with _global_gate_open(), _flippable_source_switch(ws.source) as switch_state:
            def close_after_first_response(company_key: str, name: str) -> None:
                del name
                if wire.count_company(company_key) >= 1:
                    switch_state["closed"] = True

            wire.on_served = close_after_first_response
            open_settings = _drill_settings(ws.data_dir, operational=True)
            open_worker = _make_worker(
                open_settings, fetcher, {ws.source: adapter}, approvals
            )
            first = bound[0]
            run_id = await _enqueue_run(
                listings[(first.market_code, first.ticker)].id,
                sources=(ws.source,),
                adapter_versions={ws.source: adapter.adapter_version},
                batch="shutdown-b-midrun",
            )
            await open_worker.run_once(run_id=run_id)
            row = await _run_row(run_id)
            metadata = row["metadata"]
            acquisitions = list(metadata.get("acquisitions", []))
            retained = 0
            for acquisition in acquisitions:
                capture = _verify_archive(
                    ws.data_dir, acquisition["manifest_ref"], acquisition["manifest_sha256"]
                )
                retained += len(capture.responses)
            requests_after_stop = max(0, wire.count_company(first.company_key) - 1)
            source_status = metadata.get("source_status", {}).get(ws.source)
            ledger.step(
                "close the operational source switch after the first response",
                (
                    "no further request starts, the received response is retained in the "
                    "archive/acquisition, the source is not reported as success"
                ),
                (
                    f"run status {row['status']}; source status {source_status}; requests "
                    f"served for the company {wire.count_company(first.company_key)}; "
                    f"retained archived responses {retained}; requests after stop "
                    f"{requests_after_stop}"
                ),
                passed=(
                    row["status"] == "partial"
                    and source_status not in ("saved", None)
                    and wire.count_company(first.company_key) == 1
                    and requests_after_stop == 0
                    and retained >= 1
                ),
            )
            wire.on_served = None
        records.extend(phase_a_rows)
        records.append(row)
    finally:
        await client.aclose()
    ledger.requests = wire.request_ledger
    artifact = _write_artifact(
        ws,
        "worker-run-records.json",
        "worker_run_records",
        {
            "source": ws.source,
            "phase_a": [
                {
                    "run_id": item["id"],
                    "status": item["status"],
                    "source_status": item["metadata"].get("source_status"),
                    "problems": item["metadata"].get("problems"),
                }
                for item in records[: len(phase_a_rows)]
            ],
            "phase_b": {
                "run_id": records[-1]["id"],
                "status": records[-1]["status"],
                "source_status": records[-1]["metadata"].get("source_status"),
                "problems": records[-1]["metadata"].get("problems"),
                "responses": records[-1]["metadata"].get("responses"),
                "acquisitions": records[-1]["metadata"].get("acquisitions"),
            },
        },
    )
    ledger.step(
        "restore the product gate",
        "ONLINE_REFRESH_ENABLED is False again after the drill",
        f"ONLINE_REFRESH_ENABLED={handlers.ONLINE_REFRESH_ENABLED}",
        passed=handlers.ONLINE_REFRESH_ENABLED is False,
    )
    return _finalize_evidence(
        ws,
        ledger,
        [artifact],
        service_chain=(
            "investment_agent.materials.jobs.enqueue_material_job",
            "investment_agent.research.worker.ResearchWorker.run_once",
            "investment_agent.materials.handlers.MaterialCollectionHandler",
            "investment_agent.materials.source_policy.evaluate_source_policy",
            "investment_agent.settings.loader.load_settings",
            "investment_agent.materials.archive.archive_capture",
        ),
        adapter_version=adapter.adapter_version,
        stop_semantics=DrillStopSemantics(
            stop_mechanism="operational source-refresh switch closed (before run; mid-run after first response)",
            requests_after_stop=0,
            received_responses_retained=True,
        ),
    )


# --- drill 2: inflight_stop --------------------------------------------------


def _run_inflight_stop(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.storage.database import run_with_engine

    return run_with_engine(ws.database_url, lambda: _inflight_stop_async(ws, pilot_list_path))


async def _inflight_stop_async(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.domain.security import source_symbol
    from investment_agent.materials import handlers
    from investment_agent.materials.drill_evidence import DrillStopSemantics
    from investment_agent.materials.handlers import HttpxMaterialFetcher
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.us_sec import SecFactsAdapter

    ledger = DrillLedger()
    identities, listings = await _prepare_database(pilot_list_path)
    bound = [
        identity for identity in identities if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    adapter = SecFactsAdapter() if ws.source == SEC_SOURCE else HkNativeAdapter()
    approvals = _drill_approvals(ws.source, bound, adapter.adapter_version)
    wire = MockWire(_build_catalog(ws.source, bound))
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire.respond))
    try:
        with _global_gate_open():
            fetcher = HttpxMaterialFetcher(
                client=client,
                user_agent=USER_AGENT,
                min_interval={ws.source: DRILL_MIN_INTERVAL},
            )
            before_send_calls: list[str] = []

            async def flip_gate_during_limiter_wait(request: Any) -> None:
                # Runs after the limiter released the request and before it is
                # sent: the handler's own chained hook re-checks the gate
                # immediately afterwards and stops the request in the wait.
                before_send_calls.append(request.name)
                if len(before_send_calls) >= 2:
                    handlers.ONLINE_REFRESH_ENABLED = False

            fetcher.before_send = flip_gate_during_limiter_wait
            settings = _drill_settings(ws.data_dir, operational=True)
            worker = _make_worker(settings, fetcher, {ws.source: adapter}, approvals)
            run_ids = [
                await _enqueue_run(
                    listings[(identity.market_code, identity.ticker)].id,
                    sources=(ws.source,),
                    adapter_versions={ws.source: adapter.adapter_version},
                    batch=f"inflight-{identity.company_key}",
                )
                for identity in bound
            ]
            rows = []
            for run_id in run_ids:
                await worker.run_once(run_id=run_id)
                rows.append(await _run_row(run_id))
            first = bound[0]
            first_row = rows[0]
            acquisitions = list(first_row["metadata"].get("acquisitions", []))
            retained = 0
            for acquisition in acquisitions:
                capture = _verify_archive(
                    ws.data_dir, acquisition["manifest_ref"], acquisition["manifest_sha256"]
                )
                retained += len(capture.responses)
            served_first = wire.count_company(first.company_key)
            later_companies_served = sum(
                wire.count_company(identity.company_key) for identity in bound[1:]
            )
            source_status = first_row["metadata"].get("source_status", {}).get(ws.source)
            ledger.step(
                "stop the source during its limiter wait after the first response",
                (
                    "the request waiting in the limiter is never sent and no later "
                    "request starts; already received responses are retained in the "
                    "archive/acquisition record"
                ),
                (
                    f"first company served {served_first} of its multi-response set; "
                    f"requests issued by the remaining companies {later_companies_served}; "
                    f"retained archived responses {retained}; run status "
                    f"{first_row['status']}; source status {source_status}"
                ),
                passed=(
                    served_first == 1
                    and later_companies_served == 0
                    and retained >= 1
                    and first_row["status"] == "partial"
                    and source_status not in ("saved", None)
                ),
            )
            ledger.step(
                "no run reports the stopped source as a whole-source success",
                "every affected run stays partial and no source status is 'saved'",
                f"run statuses {sorted(row['status'] for row in rows)}; "
                f"source statuses {sorted({row['metadata'].get('source_status', {}).get(ws.source) for row in rows})}",
                passed=all(row["status"] == "partial" for row in rows),
            )
    finally:
        await client.aclose()
    ledger.requests = wire.request_ledger
    artifact = _write_artifact(
        ws,
        "worker-run-records.json",
        "worker_run_records",
        {
            "source": ws.source,
            "runs": [
                {
                    "run_id": row["id"],
                    "status": row["status"],
                    "source_status": row["metadata"].get("source_status"),
                    "problems": row["metadata"].get("problems"),
                    "responses": row["metadata"].get("responses"),
                    "acquisitions": row["metadata"].get("acquisitions"),
                }
                for row in rows
            ],
        },
    )
    ledger.step(
        "restore the product gate",
        "ONLINE_REFRESH_ENABLED is False again after the drill",
        f"ONLINE_REFRESH_ENABLED={handlers.ONLINE_REFRESH_ENABLED}",
        passed=handlers.ONLINE_REFRESH_ENABLED is False,
    )
    return _finalize_evidence(
        ws,
        ledger,
        [artifact],
        service_chain=(
            "investment_agent.materials.jobs.enqueue_material_job",
            "investment_agent.research.worker.ResearchWorker.run_once",
            "investment_agent.materials.handlers.MaterialCollectionHandler",
            "investment_agent.materials.handlers.HttpxMaterialFetcher",
            "investment_agent.materials.source_policy.evaluate_source_policy",
            "investment_agent.materials.archive.archive_capture",
        ),
        adapter_version=adapter.adapter_version,
        stop_semantics=DrillStopSemantics(
            stop_mechanism="global online-refresh gate closed during the limiter wait",
            requests_after_stop=0,
            received_responses_retained=True,
        ),
    )


# --- drill 3: restart_resume -------------------------------------------------


def _run_restart_resume(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.storage.database import run_with_engine

    return run_with_engine(ws.database_url, lambda: _restart_resume_async(ws, pilot_list_path))


async def _restart_resume_async(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.domain.security import SOURCE_EASTMONEY_HK, source_symbol
    from investment_agent.materials.handlers import HttpxMaterialFetcher
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.us_sec import SecFactsAdapter

    ledger = DrillLedger()
    identities, listings = await _prepare_database(pilot_list_path)
    bound = [
        identity for identity in identities if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    adapters: dict[str, Any] = {}
    if ws.source == SEC_SOURCE:
        adapters[SEC_SOURCE] = SecFactsAdapter()
    else:
        adapters[HK_SOURCE] = HkNativeAdapter()
    other_source = (
        SEC_SOURCE if ws.source == SOURCE_EASTMONEY_HK else SOURCE_EASTMONEY_HK
    )
    # HSBC binds both controlled sources: its run exercises the checkpoint
    # across sources (the completed source is skipped on the retry), while the
    # remaining companies exercise the single-source interrupt/retry path.
    shared_identity = next(
        (identity for identity in bound if identity.company_key == "hsbc-holdings"), None
    )
    if shared_identity is not None and any(
        binding.source == other_source for binding in shared_identity.sources
    ):
        adapters[other_source] = (
            SecFactsAdapter() if other_source == SEC_SOURCE else HkNativeAdapter()
        )
    approvals_by_source: dict[str, Any] = {}
    for source, adapter in adapters.items():
        approvals_by_source.update(
            _drill_approvals(
                source,
                [
                    identity
                    for identity in identities
                    if source_symbol(identity, source) is not None
                ],
                adapter.adapter_version,
            )
        )
    wire = MockWire(_build_catalog_for_adapters(adapters, identities))
    # The transient interruption: the second document of every single-source
    # company fails once; the HSBC run fails on the second source's first
    # document, after the first source already completed and checkpointed.
    second_document = "submissions.json" if ws.source == SEC_SOURCE else "balance"
    for identity in bound:
        if shared_identity is not None and identity.company_key == shared_identity.company_key:
            wire.fail_at_occurrence[(identity.company_key, "companyfacts.json")] = 1
        else:
            wire.fail_at_occurrence[(identity.company_key, second_document)] = 1
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire.respond))
    records: list[dict[str, Any]] = []
    try:
        with _global_gate_open():
            fetcher = HttpxMaterialFetcher(
                client=client,
                user_agent=USER_AGENT,
                min_interval={source: DRILL_MIN_INTERVAL for source in adapters},
            )
            settings = _drill_settings(ws.data_dir, operational=True)
            worker = _make_worker(settings, fetcher, adapters, approvals_by_source)
            enqueued: list[tuple[int, Sequence[str], Mapping[str, str], str]] = []
            for identity in bound:
                is_shared = (
                    shared_identity is not None
                    and identity.company_key == shared_identity.company_key
                )
                sources: Sequence[str] = tuple(adapters) if is_shared else (ws.source,)
                versions = {
                    source: adapter.adapter_version for source, adapter in adapters.items()
                }
                batch = f"resume-{identity.company_key}"
                run_id = await _enqueue_run(
                    listings[(identity.market_code, identity.ticker)].id,
                    sources=sources,
                    adapter_versions=versions,
                    batch=batch,
                )
                enqueued.append((run_id, sources, versions, batch))
                await worker.run_once(run_id=run_id)
                interrupted = await _run_row(run_id)
                await worker.run_once(run_id=run_id)
                resumed = await _run_row(run_id)
                records.append(
                    {
                        "company_key": identity.company_key,
                        "sources": list(sources),
                        "interrupted": interrupted,
                        "resumed": resumed,
                    }
                )
            all_interrupted_queued = all(
                item["interrupted"]["status"] == "queued"
                and item["interrupted"]["attempt_count"] == 1
                for item in records
            )
            all_resumed_success = all(item["resumed"]["status"] == "success" for item in records)
            ledger.step(
                "interrupt each collection run mid-flight with a transient upstream failure",
                (
                    "the run is requeued for a retry (not closed as failed) and the "
                    "responses received before the interruption stay checkpointed in "
                    "the archive/acquisition records"
                ),
                (
                    f"{len(records)} interrupted runs requeued: {all_interrupted_queued}; "
                    f"acquisitions recorded before retry: "
                    f"{sum(bool(item['interrupted']['metadata'].get('acquisitions')) for item in records)}"
                ),
                passed=all_interrupted_queued
                and all(item["interrupted"]["metadata"].get("acquisitions") for item in records),
            )
            ledger.step(
                "retry every interrupted run to completion",
                "each run completes successfully on its retry",
                f"resumed statuses {sorted(item['resumed']['status'] for item in records)}",
                passed=all_resumed_success,
            )
            if shared_identity is not None:
                shared_record = next(
                    item
                    for item in records
                    if item["company_key"] == shared_identity.company_key
                )
                skipped = any(
                    entry == {"source": HK_SOURCE, "reason": "already_checked"}
                    for entry in shared_record["resumed"]["metadata"].get("skipped", [])
                )
                hsbc_first_source_requests = sum(
                    wire.count(shared_identity.company_key, name) for name in
                    ("income", "balance", "cashflow")
                )
                ledger.step(
                    "a source completed before the interruption is not fetched again",
                    (
                        "the retry skips the already-checked source (zero new requests "
                        "for it) while it finishes the interrupted one"
                    ),
                    (
                        f"first-source requests total {hsbc_first_source_requests} "
                        f"(one per table); retry skip recorded: {skipped}"
                    ),
                    passed=skipped and hsbc_first_source_requests == 3,
                )
            datasets = await _dataset_rows()
            keys: dict[tuple[Any, ...], list[Any]] = {}
            for row in datasets:
                keys.setdefault(
                    (row.subject_id, row.source, row.kind, row.period_end), []
                ).append(row)
            duplicate_keys = {
                key: len(rows) for key, rows in keys.items() if len(rows) != 1
            }
            ledger.step(
                "check the stored material for duplicate versions",
                "every subject/source/kind/period exists as exactly one version",
                f"{len(datasets)} dataset versions across {len(keys)} keys; "
                f"duplicated keys: {sorted(str(key) for key in duplicate_keys)}",
                passed=not duplicate_keys,
            )
            first_run_id, first_sources, first_versions, first_batch = enqueued[0]
            first_listing = listings[(bound[0].market_code, bound[0].ticker)].id
            rerun_id = await _enqueue_run(
                first_listing,
                sources=first_sources,
                adapter_versions=first_versions,
                batch=first_batch,
            )
            datasets_after = await _dataset_rows()
            ledger.step(
                "re-enqueue the same refresh batch",
                "no second run or second collection happens for the same batch",
                (
                    f"same run returned ({rerun_id == first_run_id}); "
                    f"dataset versions before/after re-enqueue {len(datasets)}/{len(datasets_after)}"
                ),
                passed=rerun_id == first_run_id and len(datasets) == len(datasets_after),
            )
    finally:
        await client.aclose()
    ledger.requests = wire.request_ledger
    artifact = _write_artifact(
        ws,
        "worker-run-records.json",
        "worker_run_records",
        {
            "source": ws.source,
            "runs": [
                {
                    "company_key": item["company_key"],
                    "sources": item["sources"],
                    "interrupted": {
                        "run_id": item["interrupted"]["id"],
                        "status": item["interrupted"]["status"],
                        "attempt_count": item["interrupted"]["attempt_count"],
                        "source_status": item["interrupted"]["metadata"].get("source_status"),
                        "acquisitions": item["interrupted"]["metadata"].get("acquisitions"),
                    },
                    "resumed": {
                        "run_id": item["resumed"]["id"],
                        "status": item["resumed"]["status"],
                        "source_status": item["resumed"]["metadata"].get("source_status"),
                        "skipped": item["resumed"]["metadata"].get("skipped"),
                        "saved": item["resumed"]["metadata"].get("saved"),
                    },
                }
                for item in records
            ],
        },
    )
    from investment_agent.materials import handlers as handlers_module

    ledger.step(
        "restore the product gate",
        "ONLINE_REFRESH_ENABLED is False again after the drill",
        f"ONLINE_REFRESH_ENABLED={handlers_module.ONLINE_REFRESH_ENABLED}",
        passed=handlers_module.ONLINE_REFRESH_ENABLED is False,
    )
    return _finalize_evidence(
        ws,
        ledger,
        [artifact],
        service_chain=(
            "investment_agent.materials.jobs.enqueue_material_job",
            "investment_agent.research.worker.ResearchWorker.run_once",
            "investment_agent.materials.handlers.MaterialCollectionHandler",
            "investment_agent.materials.handlers.HttpxMaterialFetcher",
            "investment_agent.materials.store.MaterialStore.save",
            "investment_agent.materials.archive.archive_capture",
            "investment_agent.materials.archive.record_acquisition",
        ),
        adapter_version=adapters[ws.source].adapter_version,
    )


def _build_catalog_for_adapters(
    adapters: Mapping[str, Any], identities: Sequence[Any]
) -> dict[str, tuple[str, str, bytes]]:
    catalog: dict[str, tuple[str, str, bytes]] = {}
    for source in adapters:
        bound = [
            identity for identity in identities
            if any(binding.source == source for binding in identity.sources)
        ]
        catalog.update(_build_catalog(source, bound))
    return catalog


# --- drill 4: old_versions_readable ------------------------------------------


def _run_old_versions(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.storage.database import run_with_engine

    return run_with_engine(ws.database_url, lambda: _old_versions_async(ws, pilot_list_path))


def _synthetic_draft(
    *, source: str, subject_id: int, adapter_version: str, value: str, period_year: int
) -> Any:
    """One contract-valid annual-facts draft (construction copied from
    tests/integration/test_material_collection_job.py's ``_draft``)."""

    from investment_agent.materials.contracts import (
        SCHEMA_VERSION,
        AccountingStandard,
        DatasetDraft,
        DatasetKind,
        DatePrecision,
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

    period = FactPeriod(
        start=date(period_year, 1, 1), end=date(period_year, 12, 31), basis=PeriodBasis.ANNUAL
    )
    fact = FactRecord(
        metric="revenue_operating",
        period=period,
        semantics=FactSemantics(
            metric="revenue_operating",
            source_label="drill synthetic label",
            source_field="drill-synthetic-field",
            accounting_standard=AccountingStandard.IFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ORDINARY,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=value,
            raw_unit="元",
            scale=1,
            unit="USD",
            currency="USD",
            sign_convention="as reported",
        ),
        provenance=FactProvenance(
            source=source,
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability="applicable",
    )
    return DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=subject_id,
        source=source,
        schema_version=SCHEMA_VERSION,
        adapter_version=adapter_version,
        captured_at=datetime.now(UTC),
        payload={"facts": [fact_to_payload(fact)], "report": "income"},
        period=period,
    )


async def _old_versions_async(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.domain.security import source_symbol
    from investment_agent.materials.archive import (
        ArchiveCapture,
        ResponseEvidence,
        archive_capture,
        identity_snapshot,
    )
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.store import MaterialStore
    from investment_agent.materials.us_sec import SecFactsAdapter
    from investment_agent.storage.database import session_scope

    ledger = DrillLedger()
    identities, listings = await _prepare_database(pilot_list_path)
    bound = [
        identity for identity in identities if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    adapter = SecFactsAdapter() if ws.source == SEC_SOURCE else HkNativeAdapter()
    identity = bound[0]
    company_id = listings[(identity.market_code, identity.ticker)].company_id
    catalog = _build_catalog(ws.source, [identity])
    first_document = next(iter(catalog.values()))
    document_name, body = first_document[1], first_document[2]
    archived = archive_capture(
        ws.data_dir,
        ArchiveCapture(
            identity=identity_snapshot(identity),
            subject_id=company_id,
            source=ws.source,
            adapter_version=OLD_ADAPTER_VERSION,
            periods=(),
            expected_requests=(document_name,),
            responses=(
                ResponseEvidence(
                    name=document_name,
                    content=body,
                    source_url="https://drill.invalid/" + document_name,
                    media_type="application/json",
                    http_status=200,
                    fetched_at=datetime.now(UTC),
                ),
            ),
        ),
    )
    capture = _verify_archive(ws.data_dir, archived.manifest_ref, archived.manifest_sha256)
    ledger.step(
        f"write an archive stamped with old adapter version {OLD_ADAPTER_VERSION!r}",
        "verify_capture still verifies manifest and body hashes and the version difference is visible",
        (
            f"verified {len(capture.responses)} response(s); archived adapter_version "
            f"{capture.adapter_version!r} vs current {adapter.adapter_version!r}"
        ),
        passed=(
            capture.adapter_version == OLD_ADAPTER_VERSION
            and capture.adapter_version != adapter.adapter_version
            and len(capture.responses) == 1
        ),
    )
    async with session_scope() as session:
        store = MaterialStore(session, data_dir=ws.data_dir)
        old_draft = _synthetic_draft(
            source=ws.source,
            subject_id=company_id,
            adapter_version=OLD_ADAPTER_VERSION,
            value="1000",
            period_year=2023,
        )
        old_saved = await store.save(old_draft)
        current_draft = _synthetic_draft(
            source=ws.source,
            subject_id=company_id,
            adapter_version=adapter.adapter_version,
            value="2000",
            period_year=2023,
        )
        current_saved = await store.save(current_draft)
        history = await store.history(
            kind=old_draft.kind,
            subject_kind=old_draft.subject_kind,
            subject_id=company_id,
        )
        latest = await store.latest(
            kind=old_draft.kind,
            subject_kind=old_draft.subject_kind,
            subject_id=company_id,
            period_end=date(2023, 12, 31),
        )
        old_row = next(row for row in history if row.id == old_saved.id)
        old_payload = dict(old_row.payload or {})
        ledger.step(
            "store an old-version dataset, then a newer version under the same key",
            "both revisions stay readable through the store and the version difference is visible",
            (
                f"revisions {[row.revision for row in history]}; latest is the current version: "
                f"{latest is not None and latest.id == current_saved.id}; old row "
                f"adapter_version {old_row.adapter_version!r} (current "
                f"{adapter.adapter_version!r}); old payload keys {sorted(old_payload)}"
            ),
            passed=(
                len(history) == 2
                and latest is not None
                and latest.id == current_saved.id
                and old_row.adapter_version == OLD_ADAPTER_VERSION
                and old_row.adapter_version != latest.adapter_version
                and bool(old_payload.get("facts"))
            ),
        )
    artifact = _write_artifact(
        ws,
        "old-version-checks.json",
        "old_version_checks",
        {
            "source": ws.source,
            "archive": {
                "manifest_ref": archived.manifest_ref,
                "manifest_sha256": archived.manifest_sha256,
                "adapter_version": capture.adapter_version,
                "current_adapter_version": adapter.adapter_version,
                "responses": len(capture.responses),
            },
            "store": {
                "old_dataset_id": old_saved.id,
                "old_revision": _revision_of(history, old_saved.id),
                "current_dataset_id": current_saved.id,
                "current_revision": _revision_of(history, current_saved.id),
                "history_rows": [
                    {
                        "id": row.id,
                        "revision": row.revision,
                        "adapter_version": row.adapter_version,
                        "content_hash": row.content_hash,
                    }
                    for row in history
                ],
            },
        },
    )
    return _finalize_evidence(
        ws,
        ledger,
        [artifact],
        service_chain=(
            "investment_agent.materials.archive.archive_capture",
            "investment_agent.materials.archive.verify_capture",
            "investment_agent.materials.store.MaterialStore.save",
            "investment_agent.materials.store.MaterialStore.history",
            "investment_agent.materials.store.MaterialStore.latest",
        ),
        adapter_version=adapter.adapter_version,
    )


def _revision_of(history: Sequence[Any], dataset_id: int) -> Any:
    return next(row.revision for row in history if row.id == dataset_id)


# --- drill 5: offline_replay --------------------------------------------------


def _run_offline_replay(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.storage.database import run_with_engine

    return run_with_engine(ws.database_url, lambda: _offline_replay_async(ws, pilot_list_path))


async def _offline_replay_async(ws: DrillWorkspace, pilot_list_path: Path) -> dict[str, object]:
    from investment_agent.domain.security import source_symbol
    from investment_agent.materials.handlers import HttpxMaterialFetcher
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.replay import replay_acquisition
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.us_sec import SecFactsAdapter
    from investment_agent.storage.database import session_scope

    ledger = DrillLedger()
    identities, listings = await _prepare_database(pilot_list_path)
    bound = [
        identity for identity in identities if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    adapter = SecFactsAdapter() if ws.source == SEC_SOURCE else HkNativeAdapter()
    approvals = _drill_approvals(ws.source, bound, adapter.adapter_version)
    wire = MockWire(_build_catalog(ws.source, bound))
    identity = bound[0]
    client = httpx.AsyncClient(transport=httpx.MockTransport(wire.respond))
    try:
        with _global_gate_open():
            fetcher = HttpxMaterialFetcher(
                client=client,
                user_agent=USER_AGENT,
                min_interval={ws.source: DRILL_MIN_INTERVAL},
            )
            settings = _drill_settings(ws.data_dir, operational=True)
            worker = _make_worker(settings, fetcher, {ws.source: adapter}, approvals)
            run_id = await _enqueue_run(
                listings[(identity.market_code, identity.ticker)].id,
                sources=(ws.source,),
                adapter_versions={ws.source: adapter.adapter_version},
                batch=f"replay-{identity.company_key}",
            )
            await worker.run_once(run_id=run_id)
            row = await _run_row(run_id)
        acquisitions = list(row["metadata"].get("acquisitions", []))
        first_saved = {
            (int(item["dataset_id"]), str(item["content_hash"]))
            for item in row["metadata"].get("saved", [])
        }
        ledger.step(
            "collect one company's material through the real worker path",
            "the run completes and stores its datasets with archived inputs",
            (
                f"run status {row['status']}; acquisitions {len(acquisitions)}; stored "
                f"datasets {len(first_saved)}"
            ),
            passed=row["status"] == "success" and bool(acquisitions) and bool(first_saved),
        )
        replay_summaries: list[dict[str, Any]] = []
        replay_matches = True
        replay_problems_clean = True
        for acquisition in acquisitions:
            for attempt in (1, 2):
                async with session_scope() as session:
                    report = await replay_acquisition(
                        session,
                        acquisition_id=int(acquisition["acquisition_id"]),
                        data_dir=ws.data_dir,
                        identities=tuple(identities),
                        adapters={ws.source: adapter},
                    )
                replay_saved = {
                    (saved.id, saved.content_hash) for saved in report.saved
                }
                matches = replay_saved == first_saved
                clean = not report.problems
                replay_matches = replay_matches and matches
                replay_problems_clean = replay_problems_clean and clean
                replay_summaries.append(
                    {
                        "acquisition_id": int(acquisition["acquisition_id"]),
                        "attempt": attempt,
                        "saved_ids": sorted(item[0] for item in replay_saved),
                        "matches_first_run": matches,
                        "problems": list(report.problems),
                        "informational": list(report.informational),
                    }
                )
        ledger.step(
            "replay the archived acquisition inputs with no network",
            "both replays rebuild exactly the first run's dataset versions and report no new problems",
            (
                f"{len(replay_summaries)} replays; matches first run: {replay_matches}; "
                f"problems free: {replay_problems_clean}"
            ),
            passed=replay_matches and replay_problems_clean,
        )
    finally:
        await client.aclose()
    ledger.requests = wire.request_ledger
    ledger.step(
        "confirm zero real network use under the socket audit hook",
        "the audit hook observed no socket connect attempt",
        f"blocked attempts: {_NETWORK_BLOCKED}",
        passed=_NETWORK_BLOCKED == 0,
    )
    artifact = _write_artifact(
        ws,
        "replay-report.json",
        "replay_report",
        {
            "source": ws.source,
            "collection_run": {
                "run_id": row["id"],
                "status": row["status"],
                "saved": row["metadata"].get("saved"),
                "acquisitions": acquisitions,
            },
            "replays": replay_summaries,
            "network_blocked_attempts": _NETWORK_BLOCKED,
        },
    )
    return _finalize_evidence(
        ws,
        ledger,
        [artifact],
        service_chain=(
            "investment_agent.materials.jobs.enqueue_material_job",
            "investment_agent.research.worker.ResearchWorker.run_once",
            "investment_agent.materials.handlers.MaterialCollectionHandler",
            "investment_agent.materials.archive.verify_capture",
            "investment_agent.materials.replay.replay_acquisition",
            "investment_agent.materials.store.MaterialStore.save",
        ),
        adapter_version=adapter.adapter_version,
    )


# --- drill 6: probe_stop_withdrawal ------------------------------------------


def _run_probe_stop_withdrawal(
    evidence_root: Path, source: str, pilot_list_path: Path
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for slug, mechanism in (
        ("probe_stop_withdrawal-stop_file", "STOP file"),
        ("probe_stop_withdrawal-licence_withdrawal", "licence withdrawal"),
    ):
        ws = _new_workspace(evidence_root, source, "probe_stop_withdrawal", slug=slug)
        # run_source_probe drives its own event loop internally, so this stays
        # a synchronous call (the mock client is closed on a fresh loop after
        # the probe returns, mirroring tests/integration/
        # test_hk206_probe_transport.py).
        entry = _probe_stop(ws, pilot_list_path, mechanism)
        entries.append({"mechanism": mechanism, **entry})
    primary, secondary = entries[0], entries[1]
    return {
        "passed": bool(primary["passed"]) and bool(secondary["passed"]),
        "evidence_path": primary["evidence_path"],
        "sha256": primary["sha256"],
        "licence_withdrawal_evidence_path": secondary["evidence_path"],
        "licence_withdrawal_sha256": secondary["sha256"],
        "failures": list(primary.get("failures", [])) + list(secondary.get("failures", [])),
    }


def _probe_stop(ws: DrillWorkspace, pilot_list_path: Path, mechanism: str) -> dict[str, object]:
    from investment_agent.domain.security import source_symbol
    from investment_agent.materials import source_probe
    from investment_agent.materials.drill_evidence import DrillStopSemantics
    from investment_agent.materials.handlers import HttpxMaterialFetcher
    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.source_observations import SOURCE_COMPANIES
    from investment_agent.materials.source_policy import evaluate_source_policy
    from investment_agent.materials.us_sec import SecFactsAdapter

    ledger = DrillLedger()
    adapter = SecFactsAdapter() if ws.source == SEC_SOURCE else HkNativeAdapter()
    bound = [
        identity
        for identity in _load_identities(pilot_list_path)
        if source_symbol(identity, ws.source) is not None
    ]
    ledger.step(
        "resolve the source's controlled companies",
        "the drill covers exactly the source's controlled company set",
        f"{sorted(identity.company_key for identity in bound)}",
        passed={identity.company_key for identity in bound} == set(SOURCE_COMPANIES[ws.source]),
    )
    approvals = _drill_approvals(ws.source, bound, adapter.adapter_version)
    wire = MockWire(_build_catalog(ws.source, bound))
    probe_dir = ws.evidence_dir / (
        "probe-stop-file" if mechanism == "STOP file" else "probe-licence-withdrawal"
    )
    stopped = {"triggered": False}
    if mechanism == "STOP file":
        wire.on_served = lambda company_key, name: _create_stop_file(
            probe_dir, stopped, company_key, name
        )
    else:
        wire.on_served = lambda company_key, name: _withdraw(stopped, company_key, name)

    def evaluator(
        source: str,
        identity: Any,
        *,
        settings: Any,
        adapter_version: Any,
        global_enabled: Any,
        approvals: Any,
    ) -> Any:
        return evaluate_source_policy(
            source,
            identity,
            settings=settings,
            adapter_version=adapter_version,
            global_enabled=global_enabled,
            approvals=None if stopped["triggered"] else approvals,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(wire.respond))
    try:
        fetcher = HttpxMaterialFetcher(
            client=client,
            user_agent=USER_AGENT,
            min_interval={ws.source: DRILL_MIN_INTERVAL},
        )
        report = source_probe.run_source_probe(
            source=ws.source,
            work_dir=probe_dir,
            user_agent=USER_AGENT,
            allow_network=True,
            periods=_hk_drill_periods() if ws.source == HK_SOURCE else None,
            dependencies=source_probe.ProbeDependencies(
                approvals=approvals,
                fetcher_factory=lambda _user_agent: fetcher,
                policy_evaluator=None if mechanism == "STOP file" else evaluator,
                pilot_list_path=pilot_list_path,
            ),
        )
    finally:
        asyncio.run(client.aclose())
    issues = [str(item) for item in report["issues"]]
    # The probe reports the stop through whichever check fired first: the
    # per-attempt STOP poll, the post-limiter licence check, or the terminal
    # stop bookkeeping. Any of them proves the mechanism stopped the source.
    expected_markers = (
        ("stop_signal", "terminal_http_status_stopped_source")
        if mechanism == "STOP file"
        else ("source_policy_closed_before_send", "probe_blocked")
    )
    requests_total = int(report["requests_total"])
    ledger.requests = [
        {
            "company_key": row.get("company_key"),
            "name": row.get("name"),
            "http_status": row.get("http_status"),
            "transport": "mock",
        }
        for row in report["requests"]
        if isinstance(row, dict)
    ]
    ledger.step(
        f"stop the isolated probe via {mechanism} after its first response",
        (
            "no further request is initiated for the source and the probe reports a "
            "failed/blocked run instead of success"
        ),
        (
            f"status {report['status']}; mode {report['mode']}; requests_total "
            f"{requests_total}; issues {sorted(set(issues))}"
        ),
        passed=(
            report["mode"] == "simulation"
            and report["status"] in ("failed", "blocked")
            and requests_total == 1
            and any(
                any(marker in issue for marker in expected_markers) for issue in issues
            )
        ),
    )
    archives = list(report["archives"])
    retained = 0
    for archive in archives:
        capture = _verify_archive(
            probe_dir, archive["manifest_ref"], archive["manifest_sha256"]
        )
        retained += len(capture.responses)
    ledger.step(
        "retain the responses received before the stop",
        "the company archive keeps every already received response body",
        f"{len(archives)} company manifest(s) holding {retained} response(s)",
        passed=len(archives) == 1 and retained == 1,
    )
    artifacts = [
        {
            "kind": "probe_report",
            "path": (probe_dir / "report.json").relative_to(ws.evidence_dir).as_posix(),
            "sha256": _sha256_file(probe_dir / "report.json"),
        }
    ]
    stop_field = (
        "probe STOP file created mid-run"
        if mechanism == "STOP file"
        else "probe licence withdrawn mid-run (injected policy)"
    )
    return _finalize_evidence(
        ws,
        ledger,
        artifacts,
        service_chain=(
            "investment_agent.materials.source_probe.run_source_probe",
            "investment_agent.materials.handlers.HttpxMaterialFetcher",
            "investment_agent.materials.source_policy.evaluate_source_policy",
            "investment_agent.materials.archive.archive_capture",
            "investment_agent.materials.archive.verify_capture",
        ),
        adapter_version=adapter.adapter_version,
        stop_semantics=DrillStopSemantics(
            stop_mechanism=stop_field,
            requests_after_stop=0,
            received_responses_retained=True,
        ),
    )


def _load_identities(pilot_list_path: Path) -> tuple[Any, ...]:
    from investment_agent.domain.security import load_pilot_identities

    return load_pilot_identities(pilot_list_path)


def _create_stop_file(probe_dir: Path, stopped: dict[str, bool], company_key: str, name: str) -> None:
    del company_key, name
    if stopped["triggered"]:
        return
    stopped["triggered"] = True
    (probe_dir / "STOP").write_text("hk206 drill stop signal\n", encoding="utf-8")


def _withdraw(stopped: dict[str, bool], company_key: str, name: str) -> None:
    del company_key, name
    stopped["triggered"] = True


# --- entry point --------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the six HK2-06 operational drills (shutdown, inflight_stop, "
            "restart_resume, old_versions_readable, offline_replay, "
            "probe_stop_withdrawal) as a socket-audited offline simulation. "
            "The evidence-root must be a new, not-yet-existing directory."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=(SEC_SOURCE, HK_SOURCE),
        help="one controlled HK2-06 source id",
    )
    parser.add_argument(
        "--evidence-root",
        required=True,
        type=Path,
        help="new, not-yet-existing directory; every drill artifact stays inside it",
    )
    parser.add_argument(
        "--drill",
        action="append",
        default=None,
        choices=DRILL_ORDER,
        help="run only this drill (repeatable); default is all six",
    )
    parser.add_argument(
        "--pilot-list",
        type=Path,
        default=None,
        help="optional pilot list override (default: the checked-in controlled list)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _install_network_audit_hook()
    args = _parser().parse_args(argv)
    from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH

    pilot_list_path = args.pilot_list or DEFAULT_PILOT_LIST_PATH
    evidence_root = args.evidence_root.expanduser().absolute()
    if evidence_root.exists() or evidence_root.is_symlink():
        print(
            f"hk206 drills: evidence-root must not already exist: {evidence_root}",
            file=sys.stderr,
        )
        return 2
    drills = args.drill or list(DRILL_ORDER)
    if len(set(drills)) != len(drills):
        print("hk206 drills: --drill repeats a drill name", file=sys.stderr)
        return 2
    evidence_root.mkdir(parents=True)
    runners: dict[str, Callable[[Path, str, Path], dict[str, object]]] = {
        "shutdown": lambda root, source, pilot: _run_shutdown(
            _new_workspace(root, source, "shutdown"), pilot
        ),
        "inflight_stop": lambda root, source, pilot: _run_inflight_stop(
            _new_workspace(root, source, "inflight_stop"), pilot
        ),
        "restart_resume": lambda root, source, pilot: _run_restart_resume(
            _new_workspace(root, source, "restart_resume"), pilot
        ),
        "old_versions_readable": lambda root, source, pilot: _run_old_versions(
            _new_workspace(root, source, "old_versions_readable"), pilot
        ),
        "offline_replay": lambda root, source, pilot: _run_offline_replay(
            _new_workspace(root, source, "offline_replay"), pilot
        ),
        "probe_stop_withdrawal": _run_probe_stop_withdrawal,
    }
    summary: dict[str, object] = {
        "schema_version": 1,
        "source": args.source,
        "mode": "simulation",
        "network_calls": 0,
        "drills": {},
    }
    drill_entries: dict[str, object] = {}
    all_passed = True
    for drill in drills:
        try:
            entry = runners[drill](evidence_root, args.source, pilot_list_path)
        except Exception as exc:  # noqa: BLE001 - a failed drill must not hide the others
            entry = {
                "passed": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": _safe_traceback(),
            }
            print(f"hk206 drills: {drill} failed: {exc}", file=sys.stderr)
        drill_entries[drill] = entry
        all_passed = all_passed and bool(entry.get("passed"))
    summary["drills"] = drill_entries
    summary_path = evidence_root / "drill-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"summary": str(summary_path), "passed": all_passed}))
    return 0 if all_passed else 2


def _safe_traceback() -> str:
    import traceback

    return traceback.format_exc()


if __name__ == "__main__":
    raise SystemExit(main())
