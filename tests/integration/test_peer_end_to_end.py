"""WP14 end-to-end offline acceptance: one migrated database, the whole chain.

Everything runs offline inside one temporary ``DATA_DIR`` over a database built
by the real migrations (alembic head, revision 048): the WP2 roster import and
WP3 annual-facts import from the run-02 fixed evidence, the WP6 offline batch
submission, the WP6 service read, the WP9 HTTP API and the WP11 read tool
driven by a fake model through the real worker. No upstream HTTP, no real LLM
(the model is a ``FunctionModel``; the source evidence is the committed
run-02 fixtures) and no real data directory is ever touched.

Scenarios (design §验收与后续开发顺序, stage D exit conditions):

* **Golden chain** - migration → identity → imports → submission → service →
  API → tool all describe the *same* submitted batch: identical calculation
  references and identical Maotai golden numbers (ROE 32.53% / 7.39% / 94.44%,
  contract-revenue 4.74% / 16.30% / 16.67%, cash-flow ratio 0.7474 / 0.5792 /
  73.33, valid peers 18/18, 18/18, 15/18).
* **Out-of-bounds refusals** - the fake model asks for an unbound peer (which
  *has* submitted results) through the full task flow and is refused; a
  fabricated calculation reference cited in the research output is refused by
  the end-to-end progress validation.
* **Forged-evidence refusals** - an upload claiming ``verified`` over a peer
  source is refused at the same save call the research path uses, and a forged
  peer-subject evidence row cited in a task's progress fails the end-to-end
  validation even though the task holds a valid peer binding.
* **Backup → restore** - ``tools/backup_research.py`` + ``verify_backup.py``
  over the chain, restored into a fresh location: roster, annual snapshots,
  saved calculations and the mapping artifact keep their UUIDs, hashes and
  references, and the WP6 service read reproduces the pre-backup result.
* **Feature flag** - off: API, agent tool and page answer the same
  ``not_supported`` / 尚未覆盖 state with the shared gap text; on: the full
  chain above is readable, and reading never enqueues a collection.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from investment_agent.agent.research import ResearchOutput, research_artifacts
from investment_agent.industry_peers.artifacts import (
    MAPPING_FILENAME,
    mapping_artifact_path,
    read_batch_mapping,
)
from investment_agent.industry_peers.calculations import METRIC_KEYS
from investment_agent.industry_peers.contracts import (
    FEATURE_DISABLED_GAP,
    PeerContextStatus,
)
from investment_agent.industry_peers.service import get_industry_peer_context
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import TaskStatus
from investment_agent.storage.database import init_engine, session_scope
from tests.integration.test_web_stock_peer_section import PEERS_JS, _labels_object
from tests.support.conversations import (
    base,
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.research import progress, response, run

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_SAMPLE = REPO_ROOT / "research/industry-data-validation/evidence/run-02/sample.json"
SAMPLE_PATH = (
    REPO_ROOT
    / "research/industry-data-validation/evidence/run-02/derived/baijiu-peer-sample.json"
)
AUDIT_PATH = (
    REPO_ROOT
    / "research/industry-data-validation/evidence/run-02/derived/baijiu-official-audit.json"
)

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
PERIOD_END = date(2025, 12, 31)
MAOTAI = "600519.SH"
UNBOUND_PEER = "000568.SZ"  # a roster member that also has submitted results


def read_cutoff() -> datetime:
    """A research binding time after everything the chain creates.

    Frozen once per test so the pre- and post-restore reads compare exactly:
    the saved metric records carry their real creation time, and the cutoff
    must not hide them (a future-created result never enters an older read).
    """

    return datetime.now(UTC) + timedelta(hours=6)

FEATURE = dict(industry_peer_context_enabled=True)
AUTH = {"X-API-Key": "e2e-secret"}

GOLDEN: dict[str, tuple[str, str, str, str, tuple[int, int]]] = {
    # metric: (target, median, position, value quantization, (valid, candidate))
    "roe": ("0.3253", "0.0739", "94.44", "exact", (18, 18)),
    "contract_revenue": ("0.0474", "0.1630", "16.67", "0.0001", (18, 18)),
    "cfo_parent_profit": ("0.7474", "0.5792", "73.33", "0.0001", (15, 18)),
}


def dec(value: str) -> Decimal:
    return Decimal(value)


def assert_golden_metrics(metrics: list[dict[str, Any]]) -> None:
    """The design's three Maotai statistics, on any entry's JSON metric shape."""

    by_key = {metric["metric_key"]: metric for metric in metrics}
    assert set(by_key) == set(METRIC_KEYS)
    for key, (target, median, position, quantum, counts) in GOLDEN.items():
        metric = by_key[key]
        assert metric["status"] == "comparable", key
        target_value = dec(metric["target_value"]["value"])
        median_value = dec(metric["peer_median"]["value"])
        if quantum == "exact":
            assert target_value == dec(target), key
            assert median_value == dec(median), key
        else:
            assert target_value.quantize(dec(quantum)) == dec(target), key
            assert median_value.quantize(dec(quantum)) == dec(median), key
        assert (
            dec(metric["ascending_position"]["value"]).quantize(dec("0.01")) == dec(position)
        ), key
        assert (metric["valid_peer_count"], metric["candidate_peer_count"]) == counts, key
        assert metric["target_value"]["unit"] == (
            "ratio" if key == "cfo_parent_profit" else "fraction"
        ), key
        assert metric["target_value"]["currency"] is None


# --- the world: migrated database + run-02 chain, one temp dir --------------------


@pytest.fixture
def peer_world(tmp_path, monkeypatch):
    """Alembic-head database and ``DATA_DIR`` inside one temporary directory.

    The environment is fully overridden (``DATABASE_URL`` / ``DATA_DIR`` /
    ``API_KEY`` / ``LLM_TIER3_API_KEY``); nothing outside ``tmp_path`` is read
    or written, and no engine ever points at a real data directory.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "app.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("API_KEY", "e2e-secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "true")
    config = AlembicConfig()
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(config, "head")
    init_engine(f"sqlite+aiosqlite:///{db_path}")
    return SimpleNamespace(data_dir=data_dir, db_path=db_path, tmp_path=tmp_path)


async def import_fixed_evidence() -> tuple[Any, Any]:
    """The WP2 roster and WP3 annual facts from the run-02 fixed evidence."""

    from investment_agent.cli.commands.peer_annual import (
        import_peer_annual_snapshots,
        load_audit_json,
        load_sample_json,
    )
    from investment_agent.cli.commands.peer_cohort import import_sample_roster
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        sample = load_sample_json(SAMPLE_PATH)
        listing_repo = ListingRepository(session)
        for entry in sample["companies"]:
            await listing_repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
        roster = await import_sample_roster(
            session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT
        )
        report = await import_peer_annual_snapshots(
            session, sample=sample, audit=load_audit_json(AUDIT_PATH), acquired_at=ACQUIRED_AT
        )
    return roster, report


async def submit_batch(data_dir: Path) -> Any:
    """The WP6 offline submission over the whole roster (the golden batch)."""

    from investment_agent.industry_peers.batch import submit_offline_batch

    async with session_scope() as session:
        return await submit_offline_batch(session, data_dir=data_dir, submitted_at=SUBMITTED_AT)


async def build_chain(data_dir: Path) -> str:
    """Imports plus submission; returns the submitted ``batch_run_id``."""

    _roster, report = await import_fixed_evidence()
    assert (report.companies, report.verification_checks) == (19, 114)
    submission = await submit_batch(data_dir)
    assert submission.created is True
    assert mapping_artifact_path(data_dir, submission.mapping.batch_run_id).is_file()
    return submission.mapping.batch_run_id


async def maotai_listing_ref() -> ObjectRef:
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(MAOTAI)
        assert listing is not None
        return ObjectRef(kind="listing", id=listing.id, revision=1)


def api_client_of() -> TestClient:
    """The real FastAPI app over the current engine's migrated database."""

    from investment_agent.api.app import create_app

    return TestClient(create_app())


def peer_tool_model(ticker: str, sink: dict):
    """A fake model: call the peer tool once, answer from the return."""

    turns = 0

    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelResponse(
                [
                    ToolCallPart(
                        "get_industry_peer_context",
                        {"business": {"ticker": ticker}},
                        tool_call_id="peer",
                    )
                ],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
            )
        for item in messages:
            for part in item.parts:
                if (
                    isinstance(part, ToolReturnPart)
                    and part.tool_name == "get_industry_peer_context"
                ):
                    sink.clear()
                    sink.update(part.content)
        return response(ResearchOutput(progress=progress()))

    return model


def insisting_model(progress_factory):
    """A fake model that keeps submitting the same invalid progress."""

    def model(messages, info):
        return response(ResearchOutput(progress=progress_factory()))

    return model


async def grant_of(task_id: UUID) -> Any:
    from investment_agent.agent.execution import ExecutionGrant
    from investment_agent.storage.models.agent import ExecutionGrantRow

    async with session_scope() as session:
        row = await session.get(ExecutionGrantRow, str(task_id))
        assert row is not None
        return ExecutionGrant.model_validate(row.payload)


async def no_collect_run_was_enqueued() -> None:
    """Reading (API, service or tool) must never enqueue a collection."""

    from sqlalchemy import func, select

    from investment_agent.storage.models.analysis import PipelineRun

    async with session_scope() as session:
        assert (
            await session.execute(
                select(func.count()).select_from(PipelineRun).where(
                    PipelineRun.kind == "industry_peer_collect"
                )
            )
        ).scalar() == 0


# --- the golden full chain --------------------------------------------------------


async def test_golden_chain_migration_to_agent_tool_reads_one_batch(peer_world) -> None:
    world = peer_world

    # WP1: the migrations built the peer tables (048 is head).
    from sqlalchemy import text

    async with session_scope() as session:
        version = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert version == "048"
        for table in ("peer_cohort_snapshot", "peer_annual_snapshot"):
            await session.execute(text(f"SELECT 1 FROM {table}"))

    # WP2 + WP3: identity-resolved roster import, 19 companies, 114 checks.
    batch_run_id = await build_chain(world.data_dir)

    # WP6: the service read reproduces the golden statistics.
    cutoff = read_cutoff()
    async with session_scope() as session:
        service = await get_industry_peer_context(
            session, ticker=MAOTAI, data_dir=world.data_dir, cutoff=cutoff
        )
    assert service.status is PeerContextStatus.PARTIAL  # run-02 has no dated facts
    assert service.batch_run_id == batch_run_id
    assert service.report_period == PERIOD_END
    assert_golden_metrics(json.loads(json.dumps(service.model_dump(mode="json")))["metrics"])
    await no_collect_run_was_enqueued()

    # WP9: the HTTP API serves the same batch with the same references.
    client = api_client_of()
    body = client.get(f"/stocks/{MAOTAI}/industry-peer-context", headers=AUTH).json()
    assert body["status"] == "partial"
    assert body["batch_run_id"] == batch_run_id
    assert body["report_period"] == PERIOD_END.isoformat()
    api_refs = {
        entry["metric_key"]: (entry["ref"]["id"], entry["ref"]["revision"])
        for entry in body["metric_calculation_refs"]
        if entry["ref"] is not None
    }
    assert set(api_refs) == set(METRIC_KEYS)
    assert all(entry["state"] == "saved" for entry in body["metric_calculation_refs"])
    assert_golden_metrics(body["metrics"])
    detail = client.get(
        f"/stocks/{MAOTAI}/industry-peer-context/companies?batch_run_id={batch_run_id}",
        headers=AUTH,
    ).json()
    assert len(detail["companies"]) == 19
    await no_collect_run_was_enqueued()

    # WP11: a fake model drives the real worker; the tool reads the bound batch.
    target = await maotai_listing_ref()
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    grant = await grant_of(receipt.task.id)
    assert grant.peer_context is not None
    # Same batch, same per-metric calculation references as the API body.
    assert grant.peer_context.batch_run_id == batch_run_id
    assert grant.peer_context.report_period == PERIOD_END
    binding_refs = {
        entry.metric_key: (str(entry.ref.id), entry.ref.revision)
        for entry in grant.peer_context.metric_calculation_refs
        if entry.ref is not None
    }
    assert binding_refs == api_refs

    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(MAOTAI, sink)), settings=base(**FEATURE)
    )
    assert task.status is TaskStatus.SUCCEEDED
    payload = sink
    assert payload["status"] == "ok"  # partial coverage stays a plain ok answer
    assert payload["status"] != "pending" and payload["dependency_id"] is None
    assert payload["data"]["coverage"] == "partial"
    assert payload["data"]["target_ticker"] == MAOTAI
    assert payload["data"]["report_period"] == PERIOD_END.isoformat()
    assert_golden_metrics(payload["data"]["metrics"])
    # Page and tool cite the same references: the tool's citable references are
    # the bound records' UUIDs, one per metric, identical to the API's mapping.
    tool_refs = {
        metric["metric_key"]: (
            metric["calculation_ref"]["id"],
            metric["calculation_ref"]["revision"],
        )
        for metric in payload["data"]["metrics"]
    }
    assert tool_refs == api_refs == binding_refs
    assert {ref["kind"] for ref in payload["refs"]} == {"calculation"}
    await no_collect_run_was_enqueued()


# --- out-of-bounds refusals through the full task flow -----------------------------


async def test_unbound_peer_read_is_refused_in_the_full_task_flow(peer_world) -> None:
    world = peer_world
    await build_chain(world.data_dir)
    target = await maotai_listing_ref()
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    grant = await grant_of(receipt.task.id)
    assert grant.peer_context is not None and grant.peer_context.target == MAOTAI

    # The unbound peer is a roster member with submitted results of its own -
    # the task boundary, not missing data, is what refuses the read.
    sink: dict = {}
    task = await run(
        receipt,
        FunctionModel(peer_tool_model(UNBOUND_PEER, sink)),
        settings=base(**FEATURE),
    )
    assert task.status is TaskStatus.FAILED
    assert sink == {}  # not one value of the unbound peer ever reached the model

    from sqlalchemy import select

    from investment_agent.storage.models.agent import ToolAction

    async with session_scope() as session:
        actions = (await session.scalars(select(ToolAction))).all()
        assert all(
            action.state != "committed"
            for action in actions
            if action.parameters.get("tool") == "get_industry_peer_context"
        )
    await no_collect_run_was_enqueued()


async def test_forged_calculation_reference_is_refused_in_the_full_task_flow(peer_world) -> None:
    world = peer_world
    await build_chain(world.data_dir)
    target = await maotai_listing_ref()
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    # The ghost reference: a calculation UUID that exists nowhere, cited as if
    # it were the peer metric record the task legitimately read about.
    ghost = ObjectRef(kind="calculation", id=uuid4(), revision=1)
    task = await run(
        receipt,
        FunctionModel(insisting_model(lambda: progress(fact_refs=(ghost,)))),
        settings=base(**FEATURE, agent_retries=1),
    )
    assert task.status is TaskStatus.FAILED
    async with session_scope() as session:
        assert not await research_artifacts(session, task.id)


# --- forged peer evidence refused end to end ---------------------------------------


async def test_forged_verified_peer_evidence_is_refused_at_the_research_save(peer_world) -> None:
    world = peer_world
    await build_chain(world.data_dir)

    from sqlalchemy import func, select

    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.foundations import InvestorError
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(MAOTAI)
        assert listing is not None
        snapshot = (
            (await session.execute(
                select(PeerAnnualSnapshot).where(PeerAnnualSnapshot.listing_id == listing.id)
            ))
            .scalars()
            .first()
        )
        assert snapshot is not None and "roe_weighted_annual" in (snapshot.fields or {})
        # The forgery: a model-supplied record claiming verified over the peer
        # annual source - the exact upload shape the research flow would accept.
        forged = EvidenceRecord(
            id=uuid4(),
            revision=1,
            created_at=datetime.now(UTC),
            actor="model",
            origin_ref=None,
            source_kind="snapshot",
            source_ref=ObjectRef(
                kind="peer_annual_snapshot",
                id=UUID(str(snapshot.id)),
                revision=1,
                field_path="fields.roe_weighted_annual",
            ),
            locator="fields.roe_weighted_annual",
            content_hash="forged-hash",
            subject_ref=ObjectRef(kind="listing", id=listing.id, revision=1),
            period=None,
            published_at=ACQUIRED_AT,
            acquired_at=ACQUIRED_AT,
            available_from=ACQUIRED_AT,
            verification="verified",
            conflict="none",
            value=Quantity(value=dec("0.3253"), unit="fraction"),
        )
        with pytest.raises(InvestorError) as error:
            await EvidenceService(session).save_evidence("local_owner", forged, uuid4())
        assert error.value.detail.code == "invalid_reference"
        # Nothing entered the store: the run-02 chain materializes no evidence
        # at all (undated facts), so no verified peer fact exists to cite.
        assert (
            await session.execute(select(func.count()).select_from(SavedEvidence))
        ).scalar() == 0


async def test_cited_forged_peer_evidence_fails_the_task_validation(peer_world) -> None:
    world = peer_world
    await build_chain(world.data_dir)
    target = await maotai_listing_ref()
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    assert (await grant_of(receipt.task.id)).peer_context is not None

    # A forged evidence row that exists in the store (whatever the bypass was):
    # verified, source = a peer annual snapshot, subject = the unbound peer.
    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.storage.models.evidence import SavedEvidence
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        peer_listing = await ListingRepository(session).get_a_share_listing(UNBOUND_PEER)
        assert peer_listing is not None
        forged = EvidenceRecord(
            id=uuid4(),
            revision=1,
            created_at=datetime.now(UTC),
            actor="model",
            origin_ref=None,
            source_kind="snapshot",
            source_ref=ObjectRef(
                kind="peer_annual_snapshot",
                id=uuid4(),
                revision=1,
                field_path="fields.roe_weighted_annual",
            ),
            locator="fields.roe_weighted_annual",
            content_hash="forged-hash",
            subject_ref=ObjectRef(kind="listing", id=peer_listing.id, revision=1),
            period=None,
            published_at=ACQUIRED_AT,
            acquired_at=ACQUIRED_AT,
            available_from=ACQUIRED_AT,
            verification="verified",
            conflict="none",
            value=Quantity(value=dec("0.9999"), unit="fraction"),
        )
        session.add(
            SavedEvidence(
                id=str(forged.id),
                revision=1,
                owner_id="local_owner",
                payload=forged.model_dump(mode="json"),
            )
        )
    forged_ref = ObjectRef(kind="evidence", id=forged.id, revision=1)

    task = await run(
        receipt,
        FunctionModel(insisting_model(lambda: progress(fact_refs=(forged_ref,)))),
        settings=base(**FEATURE),
    )
    assert task.status is TaskStatus.FAILED  # scope mismatch is terminal, no retry loop
    async with session_scope() as session:
        assert not await research_artifacts(session, task.id)


# --- backup → restore --------------------------------------------------------------


async def _peer_rows(session: Any) -> dict[str, Any]:
    """Every peer-chain row identity: cohort snapshots, facts, calculations."""

    from sqlalchemy import select

    from investment_agent.storage.models.evidence import SavedCalculation
    from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot

    cohorts = (await session.scalars(select(PeerCohortSnapshot))).all()
    annuals = (await session.scalars(select(PeerAnnualSnapshot))).all()
    calcs = (await session.scalars(select(SavedCalculation))).all()
    return {
        "cohorts": [(row.id, row.revision, row.content_hash, row.payload) for row in cohorts],
        "annuals": [
            (
                row.id,
                row.company_id,
                row.listing_id,
                row.content_hash,
                row.semantic_hash,
                row.fields,
                row.verification,
            )
            for row in annuals
        ],
        "calcs": [(row.id, row.revision, row.payload) for row in calcs],
    }


def _run_tool(script: Path, args: list[str]) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (result.stderr or result.stdout)[-2000:]


async def test_backup_restore_preserves_the_peer_chain(peer_world, tmp_path) -> None:
    world = peer_world
    batch_run_id = await build_chain(world.data_dir)
    cutoff = read_cutoff()

    async def service_read(data_dir: Path) -> dict[str, Any]:
        async with session_scope() as session:
            result = await get_industry_peer_context(
                session, ticker=MAOTAI, data_dir=data_dir, cutoff=cutoff
            )
            rows = await _peer_rows(session)
        return {"read": result.model_dump(mode="json"), "rows": rows}

    before = await service_read(world.data_dir)
    assert before["read"]["status"] == "partial"
    assert before["read"]["batch_run_id"] == batch_run_id
    assert len(before["rows"]["annuals"]) == 19
    assert len(before["rows"]["calcs"]) == 57  # 19 targets x 3 metrics
    artifact_before = mapping_artifact_path(world.data_dir, batch_run_id).read_bytes()

    # Backup with the existing tool; verify the manifest with its checker.
    backup_dir = tmp_path / "backup"
    _run_tool(
        REPO_ROOT / "tools" / "backup_research.py",
        ["--data-dir", str(world.data_dir), "--output", str(backup_dir)],
    )
    _run_tool(REPO_ROOT / "tools" / "verify_backup.py", ["--backup-dir", str(backup_dir)])
    assert (backup_dir / "research-runs" / batch_run_id / MAPPING_FILENAME).is_file()

    # Restore into a fresh location per the runbook (database + research-runs).
    restored_data_dir = tmp_path / "restored" / "data"
    restored_data_dir.mkdir(parents=True)
    shutil.copyfile(backup_dir / "app.db", restored_data_dir / "app.db")
    shutil.copytree(backup_dir / "research-runs", restored_data_dir / "research-runs")

    init_engine(f"sqlite+aiosqlite:///{restored_data_dir / 'app.db'}")
    after = await service_read(restored_data_dir)
    assert after["read"] == before["read"]  # identical status, values, refs, times
    assert after["rows"] == before["rows"]  # UUIDs, hashes and payloads unchanged
    assert (
        restored_data_dir / "research-runs" / batch_run_id / MAPPING_FILENAME
    ).read_bytes() == artifact_before
    mapping_before = read_batch_mapping(mapping_artifact_path(world.data_dir, batch_run_id))
    mapping_after = read_batch_mapping(mapping_artifact_path(restored_data_dir, batch_run_id))
    assert mapping_after == mapping_before

    # Originals: run-02 proofs carry no original ObjectRefs (original_ref is
    # None everywhere); every locator/PDF hash that exists survives the round
    # trip intact (the candidate check-only field keeps explicit Nones).
    for _id, _company, _listing, _content, _semantic, _fields, verification in after["rows"][
        "annuals"
    ]:
        for proof in verification.values():
            assert proof["original_ref"] is None
            digest = proof.get("locator_excerpt_sha256")
            assert digest is None or len(digest) == 64

    # Back to the original engine (this test's world stays consistent).
    init_engine(f"sqlite+aiosqlite:///{world.db_path}")


# --- the feature flag answers uniformly at the three entry points ------------------


async def test_flag_off_answers_not_covered_at_api_agent_and_page(peer_world, monkeypatch) -> None:
    world = peer_world
    await build_chain(world.data_dir)

    # API: the disabled body (200 business state, canonical gap, no data).
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "false")
    client = api_client_of()
    body = client.get(f"/stocks/{MAOTAI}/industry-peer-context", headers=AUTH).json()
    assert body["status"] == PeerContextStatus.NOT_SUPPORTED.value
    assert tuple(body["gaps"]) == (FEATURE_DISABLED_GAP,)
    assert body["metrics"] == []
    companies = client.get(
        f"/stocks/{MAOTAI}/industry-peer-context/companies?batch_run_id=whatever",
        headers=AUTH,
    ).json()
    assert companies["status"] == PeerContextStatus.NOT_SUPPORTED.value
    assert tuple(companies["gaps"]) == (FEATURE_DISABLED_GAP,)

    # Page: the shipped renderer maps exactly this state onto 尚未覆盖 (the
    # boot behaviour is driven in the WP10 suite over the same body); here the
    # label map itself is pinned so the three entries cannot drift apart.
    labels = _labels_object(PEERS_JS.read_text(encoding="utf-8"), "CONTEXT_STATUS_LABELS")
    assert labels[PeerContextStatus.NOT_SUPPORTED.value] == "尚未覆盖"

    # Agent admission: no tool, no binding while the flag is off.
    target = await maotai_listing_ref()
    conversation = await new_conversation()
    receipt = await submit(
        conversation, message(text="Explain this company", context_refs=(target,))
    )
    grant = await grant_of(receipt.task.id)
    assert grant.peer_context is None
    assert "get_industry_peer_context" not in grant.tool_names

    # Agent read: a task admitted while the flag was on answers the *current*
    # configuration - the not-covered state, never a value (the WP14 read gate).
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "true")
    bound_conversation = await new_conversation()
    bound_receipt = await submit(
        bound_conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    assert (await grant_of(bound_receipt.task.id)).peer_context is not None
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "false")
    trusted = await claim(bound_receipt.task)
    async with session_scope() as session:
        from investment_agent.agent.peer_tools import (
            PeerContextParameters,
            read_peer_context,
        )
        from investment_agent.agent.tasks import TaskService

        service = TaskService(session)
        action = await service.prepare_action(
            trusted,
            intent_key="tool:peer-gate",
            effect="database",
            parameters={
                "tool": "get_industry_peer_context",
                "business": {"ticker": MAOTAI},
                "phase": "research:v1",
            },
        )

        async def read(owned: Any, operation: UUID) -> dict[str, Any]:
            return await read_peer_context(
                owned, operation, PeerContextParameters(ticker=MAOTAI)
            )

        payload = await service.settle_action(trusted, UUID(action.operation_id), read)
    assert payload["status"] == "missing"
    assert payload["data"]["coverage"] == PeerContextStatus.NOT_SUPPORTED.value
    assert tuple(payload["data"]["gaps"]) == (FEATURE_DISABLED_GAP,)
    assert payload["data"]["metrics"] == []
    assert payload["refs"] == []
    flat = json.dumps(payload)
    for forbidden in ("target_value", "peer_median", "ascending_position", "0.3253"):
        assert forbidden not in flat
    await no_collect_run_was_enqueued()


async def test_flag_on_exposes_only_the_imported_results(peer_world) -> None:
    world = peer_world
    # Flag on, roster imported but nothing submitted yet: the covered target
    # reads as missing - nothing is fabricated, nothing is fetched.
    from investment_agent.cli.commands.peer_annual import load_audit_json, load_sample_json
    from investment_agent.cli.commands.peer_cohort import import_sample_roster
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        sample = load_sample_json(SAMPLE_PATH)
        for entry in sample["companies"]:
            await ListingRepository(session).ensure_a_share_listing(
                entry["code"], company_name=entry["name"]
            )
        await import_sample_roster(session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT)

    client = api_client_of()
    body = client.get(f"/stocks/{MAOTAI}/industry-peer-context", headers=AUTH).json()
    assert body["status"] == PeerContextStatus.MISSING.value
    assert body["metrics"] == []
    await no_collect_run_was_enqueued()

    # After the offline import, what the entries show comes from the submitted
    # batch alone (values pinned by the golden-chain test) - and reading still
    # never enqueues a collection.
    batch_run_id = await build_chain(world.data_dir)
    body = client.get(f"/stocks/{MAOTAI}/industry-peer-context", headers=AUTH).json()
    assert body["batch_run_id"] == batch_run_id
    assert body["status"] == "partial"
    await no_collect_run_was_enqueued()
