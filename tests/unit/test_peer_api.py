"""WP9 read-only industry-peer-context API and research binding.

Coverage follows the WP9 deliverables against the real HTTP layer (the shared
``api_client`` TestClient over a fresh disk-backed SQLite; seeds run through
``asyncio.run`` + ``session_scope``, the existing route-test pattern):

* **Golden chain** - WP2/WP3 imports plus the WP6 offline submission, read
  through ``GET /stocks/{ticker}/industry-peer-context``: ``partial`` with the
  Maotai golden values (ROE 32.53% / 7.39% / 94.44%, contract-revenue and
  CFO ratios), the earliest/latest acquisition times, check coverage, the
  excluded companies and the frozen ``metric_calculation_refs``.
* **History discipline** - ``batch_run_id`` reads exactly that batch (an older
  generation never splices the newer batch's records in); a batch that holds
  no result for the target is a 404.
* **Error contract** - non-canonical ticker or ``batch_run_id`` -> 422; an
  unknown target -> 404; every business state (missing, stale, not_supported,
  flag-disabled) is a 200 body.
* **Read-only** - the GETs enqueue nothing (the pipeline_run table stays
  empty), and they work without any research snapshot (no ``loadStockDetail``
  precondition).
* **Research binding** - the trusted binding contract validates, the HTTP
  submit path cannot set it, the worker freezes it into the run, the snapshot
  stores it verbatim, the run detail returns it (null for old snapshots), and
  ``artifact_manifest`` keeps storing file paths only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investment_agent.cli.commands.peer_annual import (
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.config import Settings
from investment_agent.industry_peers.calculations import METRIC_KEYS
from investment_agent.industry_peers.contracts import (
    BINDING_METRIC_KEYS,
    PeerAnnualField,
    PeerContextBinding,
    PeerContextStatus,
    PeerMetricBinding,
)
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.research.ifind_chain import (
    ResearchChainResult,
    ResearchExplanation,
)
from investment_agent.research.worker import ResearchJobPayload, ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_SAMPLE = REPO_ROOT / "research/industry-data-validation/evidence/run-02/sample.json"
DERIVED = REPO_ROOT / "research/industry-data-validation/evidence/run-02/derived"
SAMPLE_PATH = DERIVED / "baijiu-peer-sample.json"
AUDIT_PATH = DERIVED / "baijiu-official-audit.json"

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
LATER_ACQUIRED_AT = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
LATER_SUBMITTED_AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
PERIOD_END = date(2025, 12, 31)
MAOTAI = "600519.SH"
OTHER_TARGET = "000568.SZ"

AUTH = {"X-API-Key": "secret"}


@pytest.fixture(autouse=True)
def _peer_api_enabled(monkeypatch):
    """The read tests run with the feature flag on; the gate tests flip it off
    explicitly (same function-scoped monkeypatch, so the body wins)."""

    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "true")


def asyncio_run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- seeds: the WP2/WP3 imports and the WP6 offline submission --------------------


async def _seed_fixed_evidence(session: Any) -> None:
    """The run-02 roster (WP2) and annual facts with proofs (WP3)."""

    sample = load_sample_json(SAMPLE_PATH)
    listing_repo = ListingRepository(session)
    for entry in sample["companies"]:
        await listing_repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
    await import_sample_roster(session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT)
    await import_peer_annual_snapshots(
        session, sample=sample, audit=load_audit_json(AUDIT_PATH), acquired_at=ACQUIRED_AT
    )


async def _submit_offline(
    session: Any,
    tmp_path: Path,
    *,
    tickers: list[str] | None = None,
    submitted_at: datetime | None = None,
) -> Any:
    from investment_agent.industry_peers.batch import submit_offline_batch

    return await submit_offline_batch(
        session,
        data_dir=tmp_path,
        tickers=tickers,
        submitted_at=submitted_at or SUBMITTED_AT,
    )


def seed_fixed_evidence() -> None:
    async def seed() -> None:
        async with session_scope() as session:
            await _seed_fixed_evidence(session)

    asyncio_run(seed())


def seed_golden_chain(tmp_path: Path, *, tickers: list[str] | None = None) -> str:
    """Fixed evidence plus the offline batch submission; returns the batch id."""

    async def seed() -> str:
        async with session_scope() as session:
            await _seed_fixed_evidence(session)
            submission = await _submit_offline(session, tmp_path, tickers=tickers)
            return submission.mapping.batch_run_id

    return asyncio_run(seed())


def seed_two_generations(tmp_path: Path) -> tuple[str, str]:
    """Generation 1 (the golden batch), then a supplier refresh of one peer's
    ROE without a correction basis and the generation-2 resubmission."""

    async def seed() -> tuple[str, str]:
        async with session_scope() as session:
            await _seed_fixed_evidence(session)
            first = await _submit_offline(session, tmp_path)
            # The refresh: 000596.SZ's ROE moves to 0.99 (same provider, no
            # correction basis) - the displayed values await re-check.
            listing_repo = ListingRepository(session)
            listing = await listing_repo.get_a_share_listing("002646.SZ")
            assert listing is not None
            repo = PeerAnnualSnapshotRepository(session)
            rows = await repo.metric_versions(
                company_id=listing.company_id, metric="roe_weighted_annual"
            )
            original = rows[0]
            fields = dict(original.fields)
            entry = PeerAnnualField.model_validate(fields["roe_weighted_annual"])
            fields["roe_weighted_annual"] = entry.model_copy(
                update={"value": Decimal("0.99"), "raw_value": "99"}
            ).model_dump(mode="json")
            await repo.add(
                PeerAnnualSnapshot(
                    id=str(uuid4()),
                    company_id=original.company_id,
                    listing_id=original.listing_id,
                    report_period=original.report_period,
                    provider=original.provider,
                    adapter_version=original.adapter_version,
                    captured_at=LATER_ACQUIRED_AT,
                    content_hash="f" * 64,
                    semantic_hash="ef" * 32,
                    fields=fields,
                    verification=dict(original.verification),
                )
            )
            second = await _submit_offline(session, tmp_path, submitted_at=LATER_SUBMITTED_AT)
            assert second.created is True and second.mapping.generation == 2
            return first.mapping.batch_run_id, second.mapping.batch_run_id

    return asyncio_run(seed())


def seed_outsider_listing() -> None:
    """One directory listing outside every saved roster (not_supported case)."""

    async def seed() -> None:
        async with session_scope() as session:
            await ListingRepository(session).ensure_a_share_listing(
                "999998.SH", company_name="名单外公司"
            )

    asyncio_run(seed())


def metric_of(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(metric for metric in body["metrics"] if metric["metric_key"] == key)


def _pipeline_run_count() -> int:
    async def count() -> int:
        from sqlalchemy import func, select

        from investment_agent.storage.models.analysis import PipelineRun

        async with session_scope() as session:
            return int(
                (await session.execute(select(func.count()).select_from(PipelineRun))).scalar_one()
            )

    return asyncio_run(count())


def _research_snapshot_count() -> int:
    async def count() -> int:
        from sqlalchemy import func, select

        from investment_agent.storage.models.analysis import ResearchSnapshot

        async with session_scope() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(ResearchSnapshot)
                    )
                ).scalar_one()
            )

    return asyncio_run(count())


def _get(client: TestClient, path: str) -> Any:
    return client.get(path, headers=AUTH)


# --- golden chain over HTTP ------------------------------------------------------


def test_golden_chain_reads_partial_with_maotai_values(api_client, tmp_path) -> None:
    batch_run_id = seed_golden_chain(tmp_path)

    assert _pipeline_run_count() == 0  # nothing queued by the seeding either
    response = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context")

    assert response.status_code == 200, response.text
    body = response.json()
    assert _pipeline_run_count() == 0  # the GET never enqueues a collection
    assert body["schema_version"] == 1
    assert body["status"] == "partial"
    assert body["target"]["ticker"] == MAOTAI
    assert isinstance(body["target"]["company_id"], int)
    assert isinstance(body["target"]["listing_id"], int)
    assert body["target"]["name"]
    assert body["report_period"] == "2025-12-31"
    assert body["current_report_period"] == "2025-12-31"
    assert body["old_reference"] is False
    assert body["cohort"]["ref"]["kind"] == "peer_cohort_snapshot"
    assert body["cohort"]["ref"]["revision"] == 1
    assert len(body["cohort"]["content_hash"]) == 64
    assert body["batch_run_id"] == batch_run_id
    assert body["generation"] == 1
    assert body["batch_status"] == "success"
    assert body["provider"] == "eastmoney"
    assert body["adapter_version"] == "offline-run02-v1"

    # The frozen metric references: one saved entry per fixed metric.
    refs = {entry["metric_key"]: entry for entry in body["metric_calculation_refs"]}
    assert set(refs) == set(METRIC_KEYS)
    assert all(entry["state"] == "saved" for entry in refs.values())
    assert all(entry["ref"]["kind"] == "calculation" for entry in refs.values())

    # Golden numbers (design §验收: 茅台三项统计).
    roe = metric_of(body, "roe")
    assert roe["status"] == "comparable"
    assert Decimal(roe["target_value"]["value"]) == Decimal("0.3253")
    assert Decimal(roe["peer_median"]["value"]) == Decimal("0.0739")
    assert Decimal(roe["ascending_position"]["value"]).quantize(Decimal("0.01")) == Decimal("94.44")
    assert (roe["valid_peer_count"], roe["candidate_peer_count"]) == (18, 18)
    assert roe["target_value"]["unit"] == "fraction"
    assert roe["target_value"]["currency"] is None
    assert roe["cites_no_evidence"] is True  # run-02 facts carry no date basis
    assert roe["verification_counts"] == {}
    assert roe["calculation_ref"] is not None
    assert set(roe["result_refs"]) >= {"target", "peer_median", "ascending_position"}

    cr = metric_of(body, "contract_revenue")
    assert Decimal(cr["target_value"]["value"]).quantize(Decimal("0.0001")) == Decimal("0.0474")
    assert Decimal(cr["peer_median"]["value"]).quantize(Decimal("0.0001")) == Decimal("0.1630")
    assert Decimal(cr["ascending_position"]["value"]).quantize(Decimal("0.01")) == Decimal("16.67")
    assert (cr["valid_peer_count"], cr["candidate_peer_count"]) == (18, 18)

    cfo = metric_of(body, "cfo_parent_profit")
    assert Decimal(cfo["peer_median"]["value"]).quantize(Decimal("0.0001")) == Decimal("0.5792")
    assert (cfo["valid_peer_count"], cfo["candidate_peer_count"]) == (15, 18)
    excluded = {entry["ticker"]: entry["reason"] for entry in cfo["excluded_companies"]}
    assert excluded == {
        "000799.SZ": "non_positive_denominator",
        "000860.SZ": "non_positive_denominator",
        "600199.SH": "non_positive_denominator",
    }

    # Earliest and latest acquisition travel as separate fields (imported in
    # one pass here, so both ends match; the mixed-date shape is WP6's pin).
    assert datetime.fromisoformat(body["inputs_acquired_earliest"]) == ACQUIRED_AT
    assert datetime.fromisoformat(body["inputs_acquired_latest"]) == ACQUIRED_AT
    for key in ("data_as_of", "submitted_at"):
        parsed = datetime.fromisoformat(body[key])
        assert parsed.tzinfo is not None  # ISO8601, always with an offset
    assert body["job_hint"] is None


def test_golden_companies_endpoint_returns_fixed_version_detail(
    api_client, tmp_path
) -> None:
    batch_run_id = seed_golden_chain(tmp_path)

    response = _get(
        api_client,
        f"/stocks/{MAOTAI}/industry-peer-context/companies?batch_run_id={batch_run_id}",
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partial"
    assert body["batch_run_id"] == batch_run_id
    assert body["report_period"] == "2025-12-31"
    assert len(body["companies"]) == 19
    maotai = next(c for c in body["companies"] if c["ticker"] == MAOTAI)
    roe = next(m for m in maotai["metrics"] if m["metric_key"] == "roe")
    assert roe["is_target"] is True and roe["included"] is True
    assert Decimal(roe["value"]) == Decimal("0.3253")
    assert roe["unit"] == "fraction"
    excluded = [
        c for c in body["companies"] if any(m.get("exclusion_reason") for m in c["metrics"])
    ]
    assert {c["ticker"] for c in excluded} >= {"000799.SZ", "000860.SZ", "600199.SH"}
    # run-02 facts carry no disclosure-date basis: no materialized evidence.
    assert body["field_evidence"] == []
    assert _pipeline_run_count() == 0

    missing_param = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context/companies")
    assert missing_param.status_code == 422  # batch_run_id is required


def test_reads_work_without_any_research_snapshot(api_client, tmp_path) -> None:
    """No score, no snapshot, no prior capture: the reference still reads."""

    seed_fixed_evidence()
    assert _research_snapshot_count() == 0

    response = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context")
    assert response.status_code == 200
    assert response.json()["status"] == "missing"  # covered roster, no batch yet
    assert response.json()["metrics"] == []


# --- history discipline ----------------------------------------------------------


def test_batch_run_id_history_never_splices_the_newer_batch(
    api_client, tmp_path
) -> None:
    first_batch, _second_batch = seed_two_generations(tmp_path)

    current = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context").json()
    assert current["generation"] == 2
    assert current["status"] == "stale"  # the supplier refresh awaits re-check
    assert any("002646.SZ" in reason for reason in current["stale_reasons"])
    current_roe = metric_of(current, "roe")
    assert Decimal(current_roe["peer_median"]["value"]) != Decimal("0.0739")

    history = _get(
        api_client,
        f"/stocks/{MAOTAI}/industry-peer-context?batch_run_id={first_batch}",
    )
    assert history.status_code == 200
    history_body = history.json()
    # Exactly the old batch's mapping: generation 1, the golden median, and no
    # re-check flag - history reports the past as it was.
    assert history_body["batch_run_id"] == first_batch
    assert history_body["generation"] == 1
    assert history_body["stale_reasons"] == []
    history_roe = metric_of(history_body, "roe")
    assert Decimal(history_roe["peer_median"]["value"]) == Decimal("0.0739")
    assert history_roe["calculation_ref"] != current_roe["calculation_ref"]
    assert _pipeline_run_count() == 0


def test_batch_of_another_target_is_a_404(api_client, tmp_path) -> None:
    other_batch = seed_golden_chain(tmp_path, tickers=[OTHER_TARGET])

    response = _get(
        api_client, f"/stocks/{MAOTAI}/industry-peer-context?batch_run_id={other_batch}"
    )
    assert response.status_code == 404

    companies = _get(
        api_client,
        f"/stocks/{MAOTAI}/industry-peer-context/companies?batch_run_id={other_batch}",
    )
    assert companies.status_code == 404

    # The batch's own target still reads it normally.
    own = _get(
        api_client,
        f"/stocks/{OTHER_TARGET}/industry-peer-context?batch_run_id={other_batch}",
    )
    assert own.status_code == 200
    assert own.json()["batch_run_id"] == other_batch


# --- error contract --------------------------------------------------------------


def test_non_canonical_inputs_are_422(api_client, tmp_path) -> None:
    seed_fixed_evidence()

    assert _get(api_client, "/stocks/600519/industry-peer-context").status_code == 422
    assert _get(api_client, "/stocks/AABBCC.SZ/industry-peer-context").status_code == 422
    assert (
        _get(
            api_client,
            f"/stocks/{MAOTAI}/industry-peer-context?batch_run_id=../evil",
        ).status_code
        == 422
    )
    assert (
        _get(
            api_client, "/stocks/600519/industry-peer-context/companies?batch_run_id=x"
        ).status_code
        == 422
    )


def test_unknown_target_is_404(api_client, tmp_path) -> None:
    seed_fixed_evidence()

    response = _get(api_client, "/stocks/999999.SH/industry-peer-context")
    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]


def test_outside_the_roster_is_a_not_supported_business_state(
    api_client, tmp_path
) -> None:
    seed_fixed_evidence()
    seed_outsider_listing()

    response = _get(api_client, "/stocks/999998.SH/industry-peer-context")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_supported"
    assert body["target"]["ticker"] == "999998.SH"
    assert any("not a member of any saved peer cohort" in gap for gap in body["gaps"])


def test_no_submitted_batch_is_a_missing_business_state(api_client, tmp_path) -> None:
    seed_fixed_evidence()

    response = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "missing"
    assert any("no submitted peer batch" in gap for gap in body["gaps"])
    assert body["metrics"] == []
    # The pending-job slot exists structurally; a GET never invents job state.
    assert body["job_hint"] is not None
    assert body["job_hint"]["has_pending_job"] is None


def test_the_eight_business_states_are_the_designs_enum() -> None:
    assert {state.value for state in PeerContextStatus} == {
        "ready",
        "partial",
        "pending",
        "not_supported",
        "missing",
        "conflict",
        "stale",
        "failed",
    }


# --- feature gate (WP14 unifies enforcement; WP9 is the API-side minimal gate) ----


def test_feature_disabled_returns_the_not_covered_state(api_client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "false")
    seed_golden_chain(tmp_path)  # data exists; the gate answers before the DB

    response = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_supported"
    assert body["target"] is None
    assert body["metrics"] == []
    assert any("disabled" in gap for gap in body["gaps"])

    companies = _get(
        api_client,
        f"/stocks/{MAOTAI}/industry-peer-context/companies?batch_run_id=whatever",
    )
    assert companies.status_code == 200
    assert companies.json()["status"] == "not_supported"


def test_feature_enabled_reads_normally(api_client, tmp_path) -> None:
    seed_golden_chain(tmp_path)

    response = _get(api_client, f"/stocks/{MAOTAI}/industry-peer-context")
    assert response.status_code == 200
    assert response.json()["status"] == "partial"


# --- research binding (WP9) ------------------------------------------------------


def test_binding_metric_keys_match_the_calculation_layer() -> None:
    assert BINDING_METRIC_KEYS == METRIC_KEYS


def test_binding_contract_rejects_broken_shapes() -> None:
    base = _binding_payload()
    assert PeerContextBinding.model_validate(base) is not None  # the happy shape

    incomplete = {key: value for key, value in base.items() if key != "cutoff"}
    with pytest.raises(ValueError):
        PeerContextBinding.model_validate(incomplete)
    naive_cutoff = {**base, "cutoff": "2026-10-12T08:00:00"}
    with pytest.raises(ValueError):
        PeerContextBinding.model_validate(naive_cutoff)
    wrong_keys = {
        **base,
        "metric_calculation_refs": [
            {"metric_key": "pe", "state": "gap", "ref": None, "gap_reason": "x"},
            {"metric_key": "pb", "state": "gap", "ref": None, "gap_reason": "x"},
            {"metric_key": "ps", "state": "gap", "ref": None, "gap_reason": "x"},
        ],
    }
    with pytest.raises(ValueError, match="three fixed metric keys"):
        PeerContextBinding.model_validate(wrong_keys)
    foreign_cohort = {
        **base,
        "cohort_ref": {"kind": "calculation", "id": str(uuid4()), "revision": 1},
    }
    with pytest.raises(ValueError, match="peer_cohort_snapshot"):
        PeerContextBinding.model_validate(foreign_cohort)


async def test_binding_builder_freezes_a_real_read(db_session, tmp_path) -> None:
    await _seed_fixed_evidence(db_session)
    submission = await _submit_offline(db_session, tmp_path, tickers=[MAOTAI])
    await db_session.commit()

    result = await get_industry_peer_context(db_session, ticker=MAOTAI, data_dir=tmp_path)
    binding = peer_context_binding_from(result)
    assert binding is not None
    assert binding.target == MAOTAI
    assert binding.batch_run_id == submission.mapping.batch_run_id
    assert binding.report_period == PERIOD_END
    assert binding.cutoff == result.data_as_of  # the binding time is the read time
    assert sorted(entry.metric_key for entry in binding.metric_calculation_refs) == sorted(
        METRIC_KEYS
    )
    saved = next(entry for entry in binding.metric_calculation_refs if entry.metric_key == "roe")
    assert saved.state == "saved" and saved.ref is not None
    # Round-trips through the exact JSON shape the snapshot column stores.
    assert PeerContextBinding.model_validate(binding.model_dump(mode="json")) == binding

    # Without a submitted batch there is nothing to bind (a recorded gap stays
    # a gap - it is never bound to a newer batch later).
    empty = await get_industry_peer_context(
        db_session, ticker=MAOTAI, data_dir=tmp_path / "no-batches"
    )
    assert peer_context_binding_from(empty) is None


async def test_snapshot_stores_the_binding_verbatim(db_session) -> None:
    payload = _binding_payload()
    run = await PipelineRunRepository(db_session).create(kind="ifind_research")
    await ResearchSnapshotRepository(db_session).create(
        pipeline_run_id=run.id,
        symbol="600519",
        market="SH",
        company_name="贵州茅台",
        industry="食品饮料",
        as_of=date(2026, 10, 5),
        report_year=2025,
        canonical_schema_version="ifind-canonical.v1",
        scoring_version="v1.2",
        score_schema_version="v2.0",
        score_json={"business_score": 70.0},
        explanation_json=None,
        artifact_manifest={"scores": "/tmp/scores.json"},
        industry_peer_context=payload,
    )

    loaded = await ResearchSnapshotRepository(db_session).by_run(run.id)
    assert loaded is not None
    assert loaded.industry_peer_context == payload


async def test_snapshot_without_binding_stays_null(db_session) -> None:
    run = await PipelineRunRepository(db_session).create(kind="ifind_research")
    await ResearchSnapshotRepository(db_session).create(
        pipeline_run_id=run.id,
        symbol="600519",
        market="SH",
        company_name="贵州茅台",
        industry="食品饮料",
        as_of=date(2026, 10, 5),
        report_year=2025,
        canonical_schema_version="ifind-canonical.v1",
        scoring_version="v1.2",
        score_schema_version="v2.0",
        score_json={"business_score": 70.0},
        explanation_json=None,
        artifact_manifest={},
    )

    loaded = await ResearchSnapshotRepository(db_session).by_run(run.id)
    assert loaded is not None
    assert loaded.industry_peer_context is None


def test_run_detail_returns_binding_null_and_verbatim(api_client, tmp_path) -> None:
    payload = _binding_payload()
    bound_run, old_run = _seed_two_snapshots(payload)

    bound = _get(api_client, f"/research/runs/{bound_run}")
    assert bound.status_code == 200
    assert bound.json()["industry_peer_context"] == payload

    old = _get(api_client, f"/research/runs/{old_run}")
    assert old.status_code == 200
    assert old.json()["industry_peer_context"] is None  # old snapshots: never backfilled


def _seed_two_snapshots(payload: dict[str, Any]) -> tuple[int, int]:
    async def seed() -> tuple[int, int]:
        async with session_scope() as session:
            runs = PipelineRunRepository(session)
            snapshots = ResearchSnapshotRepository(session)
            bound = await runs.create(kind="ifind_research")
            old = await runs.create(kind="ifind_research")
            for run_id in (bound.id, old.id):
                await snapshots.create(
                    pipeline_run_id=run_id,
                    symbol="600519",
                    market="SH",
                    company_name="贵州茅台",
                    industry="食品饮料",
                    as_of=date(2026, 10, 5),
                    report_year=2025,
                    canonical_schema_version="ifind-canonical.v1",
                    scoring_version="v1.2",
                    score_schema_version="v2.0",
                    score_json={"business_score": 70.0},
                    explanation_json=None,
                    artifact_manifest={},
                    industry_peer_context=payload if run_id == bound.id else None,
                )
            return bound.id, old.id

    return asyncio.run(seed())


def test_http_submit_cannot_set_the_binding(api_client) -> None:
    response = api_client.post(
        "/research/single-stock",
        headers=AUTH,
        json={
            "ticker": MAOTAI,
            "provider": "snapshot",
            "industry_peer_context": _binding_payload(),
        },
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    async def read_request() -> dict[str, object]:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).get(run_id)
            assert run is not None
            return dict(run.request_json)

    request_json = asyncio_run(read_request())
    assert "industry_peer_context" not in request_json


async def test_worker_freezes_a_valid_binding_into_the_run(db_session, tmp_path) -> None:
    del db_session  # initializes the shared in-memory engine and schema
    run_id = await _enqueue_research({"industry_peer_context": _binding_payload()})
    captured: dict[str, object] = {}

    async def fake_runner(**kwargs: object) -> ResearchChainResult:
        captured.update(kwargs)
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return _fake_chain_result(run_id)

    worker = ResearchWorker(
        settings=_worker_settings(tmp_path), worker_id="binding-worker", runner=fake_runner
    )
    assert await worker.run_once() == run_id
    assert captured["industry_peer_context"] is not None
    assert captured["industry_peer_context"]["target"] == MAOTAI
    assert captured["industry_peer_context"]["batch_run_id"].startswith("peer-offline-")
    assert len(captured["industry_peer_context"]["metric_calculation_refs"]) == 3


async def test_worker_rejects_a_run_with_an_arbitrary_binding(db_session, tmp_path) -> None:
    del db_session  # initializes the shared in-memory engine and schema
    run_id = await _enqueue_research({"industry_peer_context": {"target": MAOTAI}})
    worker = ResearchWorker(settings=_worker_settings(tmp_path), worker_id="binding-worker")
    assert await worker.run_once() == run_id

    async with session_scope() as session:
        persisted = await PipelineRunRepository(session).get(run_id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error_summary == "ValidationError"


async def _enqueue_research(extra: dict[str, object]) -> int:
    async with session_scope() as session:
        run, _created = await PipelineRunRepository(session).enqueue(
            kind="ifind_research",
            idempotency_key=f"binding-{uuid4()}",
            triggered_by="test",
            request_json={
                "ticker": MAOTAI,
                "industry": "食品饮料",
                "report_year": 2025,
                "as_of": "2026-10-05",
                **extra,
            },
        )
        return run.id


def _worker_settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
    )


def _fake_chain_result(run_id: int) -> ResearchChainResult:
    return ResearchChainResult(
        run_id=run_id,
        artifact_dir=Path("/tmp") / f"chain-{run_id}",
        score={},
        explanation=ResearchExplanation(
            summary="fixture",
            classification_rationale=[],
            positive_evidence=[],
            negative_evidence=[],
            risk_factors=[],
            data_quality_notes=[],
            open_questions=[],
            workflow_conclusion="needs_review",
        ),
    )


def test_payload_validation_accepts_only_real_bindings() -> None:
    payload = _binding_payload()
    valid = ResearchJobPayload.model_validate(
        {
            "ticker": MAOTAI,
            "industry": "食品饮料",
            "report_year": 2025,
            "as_of": "2026-10-05",
            "industry_peer_context": payload,
        }
    )
    assert valid.industry_peer_context == payload

    old_shape = ResearchJobPayload.model_validate(  # pre-field payloads: unchanged
        {"ticker": MAOTAI, "industry": "食品饮料", "report_year": 2025, "as_of": "2026-10-05"}
    )
    assert old_shape.industry_peer_context is None

    with pytest.raises(ValueError, match="peer-context binding"):
        ResearchJobPayload.model_validate(
            {
                "ticker": MAOTAI,
                "industry": "食品饮料",
                "report_year": 2025,
                "as_of": "2026-10-05",
                "industry_peer_context": {"target": MAOTAI, "batch_run_id": "x"},
            }
        )


# --- artifact_manifest keeps storing file paths only ------------------------------


async def test_artifact_manifest_rejects_object_ref_and_uuid_values(db_session) -> None:
    run = await PipelineRunRepository(db_session).create(kind="ifind_research")
    repository = ResearchSnapshotRepository(db_session)

    async def attempt(manifest: dict[str, object]) -> None:
        await repository.create(
            pipeline_run_id=run.id,
            symbol="600519",
            market="SH",
            company_name="贵州茅台",
            industry="食品饮料",
            as_of=date(2026, 10, 5),
            report_year=2025,
            canonical_schema_version="ifind-canonical.v1",
            scoring_version="v1.2",
            score_schema_version="v2.0",
            score_json={"business_score": 70.0},
            explanation_json=None,
            artifact_manifest=manifest,
        )

    with pytest.raises(ValueError, match="paths only"):
        await attempt(
            {"mapping": {"kind": "calculation", "id": str(uuid4()), "revision": 1}}
        )
    with pytest.raises(ValueError, match="paths only"):
        await attempt({"scores": uuid4()})


def _binding_payload() -> dict[str, Any]:
    """A valid trusted binding, exactly as the app would freeze it."""

    binding = PeerContextBinding(
        target=MAOTAI,
        cohort_ref=ObjectRef(kind="peer_cohort_snapshot", id=uuid4(), revision=1),
        batch_run_id="peer-offline-20261012T080000Z-deadbeef",
        report_period=PERIOD_END,
        cutoff=datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
        metric_calculation_refs=(
            PeerMetricBinding(
                metric_key="roe",
                state="saved",
                ref=ObjectRef(kind="calculation", id=uuid4(), revision=1),
            ),
            PeerMetricBinding(
                metric_key="contract_revenue",
                state="reused",
                ref=ObjectRef(kind="calculation", id=uuid4(), revision=1),
            ),
            PeerMetricBinding(
                metric_key="cfo_parent_profit",
                state="gap",
                gap_reason="no usable source fact for the metric",
            ),
        ),
    )
    return binding.model_dump(mode="json")
