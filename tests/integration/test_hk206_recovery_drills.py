"""HK2-06 isolation recovery and version drills (simulation only).

The observation plan (``docs/product/plans/hk-us-expansion/35-hk2-06-observation-plan.md``)
names six independent drills; their ids live in the ``_DRILLS`` tuple of
:mod:`investment_agent.materials.source_observations`. This module proves, with
the real service chain (``enqueue_material_job`` -> ``MaterialCollectionHandler``
-> ``MaterialStore`` -> ``archive_capture``/``record_acquisition`` ->
``replay_acquisition``, probes through ``run_source_probe``) and synthetic
traffic only, the drills that are offline-provable:

* ``restart_resume`` — a mid-run interruption keeps every finished source and
  every already-received response; the retried attempt resumes past them and
  re-issues none of the finished source's requests;
* ``old_versions_readable`` — identical facts never create a second version; a
  legitimate revision creates one while the old version, its raw artifact and
  its acquisition links stay readable; a re-fetched identical body (new
  ``fetched_at``) creates nothing;
* ``offline_replay`` — after relocating the database and the original-response
  packages to a new directory and deleting the originals, replaying an
  acquisition with sockets blocked reproduces the same facts and version
  identity, and corrupted or missing archive bodies are reported, not guessed
  around (manifest tampering is already covered by
  ``test_material_replay_rejections`` and is not repeated here);
* ``probe_stop_withdrawal`` — the one-shot probe in simulation mode (injected
  ``fetcher_factory``) creates no pipeline run, no dataset version and no
  checkpoint files, honors its STOP signal by stopping further requests, and
  has no resume semantics or checkpoint/resume API.

``shutdown`` / ``inflight_stop`` (the daily-worker switch drills) and the real
probe STOP/licence-withdrawal evidence stay with HK2-05 and the real isolated
probes; nothing here opens a socket or mutates product state.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import socket
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from investment_agent.domain.security import SOURCE_EASTMONEY_HK
from investment_agent.materials import handlers, source_observations, source_probe
from investment_agent.materials.archive import verify_capture
from investment_agent.materials.contracts import (
    DatasetKind,
    RawPayload,
    SubjectKind,
    content_hash,
)
from investment_agent.materials.handlers import (
    SKIPPED_ALREADY_CHECKED,
    MaterialCollectionHandler,
)
from investment_agent.materials.jobs import (
    MaterialCollectionPayload,
    enqueue_material_job,
)
from investment_agent.materials.replay import ReplayError, replay_acquisition
from investment_agent.materials.store import MaterialStore, coalesce_annual_tables
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.research_material import ResearchDataset
from tests.integration._hk206_drill_support import (
    HK_REQUEST_NAME,
    PROBE_UA,
    SEC_REQUEST_NAME,
    SIM_ADAPTER_VERSION,
    ProbeStubFetcher,
    ScriptedFetcher,
    SyntheticAnnualAdapter,
    acquisition_rows,
    copy_sqlite,
    dataset_rows,
    dataset_signature,
    hsbc_two_source_approvals,
    one_hk_pilot_list,
    probe_licensing_approval,
    sim_body,
    sim_payload,
    transient_with_received_body,
)
from tests.integration.test_material_collection_job import (
    HK_SOURCE,
    HSBC,
    SEC_SOURCE,
    _claim,
    _Ctx,
    _identity,
    _listing_id,
    _run_row,
    _seed,
    _settings,
)
from tests.unit import test_material_source_probe as probe_fixtures

DRILLS_EXERCISED: tuple[str, ...] = (
    "restart_resume",
    "old_versions_readable",
    "offline_replay",
    "probe_stop_withdrawal",
)


def test_drill_names_come_from_the_observation_plan_vocabulary() -> None:
    """Every drill this module claims is spelled exactly as ``_DRILLS`` has it."""

    assert set(DRILLS_EXERCISED) <= set(source_observations._DRILLS)
    assert len(set(DRILLS_EXERCISED)) == len(DRILLS_EXERCISED)
    assert set(source_observations._DRILLS) == {
        "shutdown",
        "inflight_stop",
        "restart_resume",
        "old_versions_readable",
        "offline_replay",
        "probe_stop_withdrawal",
    }


def _licensed_payload(
    listing_id: int, batch: str, sources: tuple[str, ...]
) -> MaterialCollectionPayload:
    return MaterialCollectionPayload(
        listing_id=listing_id,
        sources=sources,
        refresh_batch=batch,
        adapter_versions={source: SIM_ADAPTER_VERSION for source in sources},
    )


# --- drill: restart_resume --------------------------------------------------


async def test_interrupted_run_resumes_without_refetching_finished_sources(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drill: restart_resume (simulation).

    A two-source run is interrupted by a transient HTTP 500 in the second
    source after the first finished. The retry must resume: the finished
    source is skipped (``already_checked``) with zero new fetches, the run
    completes, and every response received before the interruption — the
    finished source's capture and the interrupted source's 500 body — is still
    archived and linked after the restart.
    """

    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = ScriptedFetcher(
        script={
            HK_REQUEST_NAME: [sim_payload(HK_REQUEST_NAME, 1000)],
            SEC_REQUEST_NAME: [
                transient_with_received_body(SEC_REQUEST_NAME, 500),
                sim_payload(SEC_REQUEST_NAME, 2000),
            ],
        }
    )
    settings = _settings(tmp_path)
    handler = MaterialCollectionHandler(
        settings=settings,
        fetcher=fetcher,
        adapters={
            HK_SOURCE: SyntheticAnnualAdapter(source=HK_SOURCE, request_name=HK_REQUEST_NAME),
            SEC_SOURCE: SyntheticAnnualAdapter(source=SEC_SOURCE, request_name=SEC_REQUEST_NAME),
        },
        approvals=hsbc_two_source_approvals(),
    )
    payload = _licensed_payload(listing_id, "hk206-restart-resume", (HK_SOURCE, SEC_SOURCE))
    async with session_scope() as session:
        run, created = await enqueue_material_job(session, payload, triggered_by="test")
        assert created is True
        run_id = run.id
    worker = ResearchWorker(
        settings=settings,
        worker_id="hk206-drill-worker",
        handlers=[handler],
        retry_base_delay_seconds=0.0,
    )

    # Attempt 1: the first source is stored; the second is interrupted.
    assert await worker.run_once() == run_id
    run = await _run_row(run_id)
    assert run.status == "queued"  # one retry slot spent, no verdict yet
    assert run.attempt_count == 1
    assert fetcher.calls == [HK_REQUEST_NAME, SEC_REQUEST_NAME]
    metadata = run.metadata_json
    assert metadata["checked"] == [HK_SOURCE]
    assert metadata["source_status"] == {HK_SOURCE: "saved", SEC_SOURCE: "transient_error"}
    assert any(
        problem.startswith(f"{SEC_SOURCE}: transient_error:") for problem in metadata["problems"]
    )
    rows = await dataset_rows()
    assert len(rows) == 1
    assert rows[0].subject_id == companies[HSBC]
    interrupted = await acquisition_rows()
    assert [row.source for row in interrupted] == [HK_SOURCE, SEC_SOURCE]
    interrupted_sec = interrupted[1]
    assert interrupted_sec.dataset_links == []  # nothing was parsed from the 500
    kept = verify_capture(
        tmp_path, interrupted_sec.archive_ref, expected_sha256=interrupted_sec.manifest_sha256
    )
    (kept_response,) = kept.responses
    assert kept_response.http_status == 500
    assert kept_response.content == sim_body(500)
    assert kept_response.parse_eligible is False

    # Re-registering the same request hits the same queued run (no duplicate).
    async with session_scope() as session:
        again, created_again = await enqueue_material_job(session, payload, triggered_by="test")
        assert created_again is False
        assert again.id == run_id

    # Attempt 2: the finished source is resumed past, never re-fetched.
    assert await worker.run_once() == run_id
    assert fetcher.calls == [HK_REQUEST_NAME, SEC_REQUEST_NAME, SEC_REQUEST_NAME]

    run = await _run_row(run_id)
    assert run.status == "success"
    metadata = run.metadata_json
    assert metadata["checked"] == [HK_SOURCE, SEC_SOURCE]
    assert metadata["source_status"] == {HK_SOURCE: "saved", SEC_SOURCE: "saved"}
    assert metadata["skipped"] == [{"source": HK_SOURCE, "reason": SKIPPED_ALREADY_CHECKED}]
    assert metadata["problems"] == []  # the transient problem was cleared on resume

    rows = await dataset_rows()
    assert len(rows) == 2  # one version per source, no re-store of the finished one
    acquisitions = await acquisition_rows()
    assert [row.source for row in acquisitions] == [HK_SOURCE, SEC_SOURCE, SEC_SOURCE]
    # Nothing recorded before the interruption was lost or rewritten.
    assert acquisitions[0].manifest_sha256 == interrupted[0].manifest_sha256
    assert acquisitions[1].manifest_sha256 == interrupted_sec.manifest_sha256
    assert acquisitions[2].dataset_links  # the retry's successful capture is linked


# --- drill: old_versions_readable (version identity side) -------------------


async def test_identical_facts_never_create_a_second_version(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drill: old_versions_readable (simulation) — identical facts, one version.

    The same digest re-saved through ``MaterialStore`` with later timestamps,
    and the same body re-collected under a new refresh batch, both resolve to
    the existing version: ``created=False``, unchanged revision, untouched raw
    artifact.
    """

    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = ScriptedFetcher(script={HK_REQUEST_NAME: [sim_payload(HK_REQUEST_NAME, 1000)]})
    adapter = SyntheticAnnualAdapter(source=HK_SOURCE)
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={HK_SOURCE: adapter},
        approvals=hsbc_two_source_approvals(),
    )

    job, _ = await _claim(_licensed_payload(listing_id, "hk206-dedup-a", (HK_SOURCE,)))
    assert await handler.execute(job, _Ctx()) == "completed"
    (row,) = await dataset_rows()
    assert row.revision == 1
    assert row.raw_ref is not None
    artifact = tmp_path / row.raw_ref
    artifact_mtime = artifact.stat().st_mtime_ns

    # The store: a same-digest draft with different timestamps is a no-op.
    emitted = adapter.drafts[0]
    later = datetime.now(UTC) + timedelta(days=3)
    resave = replace(
        coalesce_annual_tables((emitted,))[0],
        raw_bytes=sim_body(1000),  # the handler's single-response attach rule
        captured_at=later,
        source_fetched_at=later,
    )
    assert content_hash(resave) == row.content_hash  # timestamps are not identity
    async with session_scope() as session:
        outcome = await MaterialStore(session, data_dir=tmp_path).save(resave)
    assert outcome.created is False
    assert (outcome.id, outcome.revision, outcome.content_hash) == (
        row.id,
        1,
        row.content_hash,
    )

    # The full chain: re-collecting the same body under a new batch adds nothing.
    fetcher.script[HK_REQUEST_NAME].append(sim_payload(HK_REQUEST_NAME, 1000))
    job, rerun_id = await _claim(_licensed_payload(listing_id, "hk206-dedup-b", (HK_SOURCE,)))
    assert await handler.execute(job, _Ctx()) == "completed"

    rows = await dataset_rows()
    assert len(rows) == 1
    assert (await _run_row(rerun_id)).metadata_json["saved"] == [
        {
            "source": HK_SOURCE,
            "dataset_id": row.id,
            "created": False,
            "content_hash": row.content_hash,
        }
    ]
    assert artifact.stat().st_mtime_ns == artifact_mtime


# --- drill: old_versions_readable (revision side) ---------------------------


async def test_a_legitimate_revision_keeps_the_old_version_readable(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drill: old_versions_readable (simulation) — revision and references.

    A revised number in the response creates a new version with a revision
    chain; the old version stays readable through ``history``/by id, its raw
    artifact is untouched, and the first acquisition's dataset links still
    resolve to the old version. A third collection whose body equals the
    revision (only ``fetched_at`` moved) creates no further version.
    """

    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = ScriptedFetcher(
        script={
            HK_REQUEST_NAME: [
                sim_payload(HK_REQUEST_NAME, 1000),
                sim_payload(HK_REQUEST_NAME, 1100),
                sim_payload(HK_REQUEST_NAME, 1100),
            ]
        }
    )
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={HK_SOURCE: SyntheticAnnualAdapter(source=HK_SOURCE)},
        approvals=hsbc_two_source_approvals(),
    )

    job, _ = await _claim(_licensed_payload(listing_id, "hk206-rev-a", (HK_SOURCE,)))
    assert await handler.execute(job, _Ctx()) == "completed"
    (original,) = await dataset_rows()
    assert original.revision == 1
    assert original.raw_ref is not None
    original_artifact = tmp_path / original.raw_ref
    original_bytes = original_artifact.read_bytes()
    (first_acquisition,) = await acquisition_rows()
    assert first_acquisition.dataset_links == [
        {"id": original.id, "revision": 1, "content_hash": original.content_hash}
    ]

    # The issuer revises the number: one new version, chain intact.
    job, _ = await _claim(_licensed_payload(listing_id, "hk206-rev-b", (HK_SOURCE,)))
    assert await handler.execute(job, _Ctx()) == "completed"
    async with session_scope() as session:
        store = MaterialStore(session, data_dir=tmp_path)
        history = await store.history(
            kind=DatasetKind.ANNUAL_FACTS,
            subject_kind=SubjectKind.COMPANY,
            subject_id=companies[HSBC],
        )
        latest = await store.latest(
            kind=DatasetKind.ANNUAL_FACTS,
            subject_kind=SubjectKind.COMPANY,
            subject_id=companies[HSBC],
            period_end=date(2024, 12, 31),
        )
        old_row = await session.get(ResearchDataset, original.id)
    assert old_row is not None
    assert latest is not None
    assert [row.revision for row in history] == [1, 2]
    revised = history[1]
    assert revised.id != original.id
    assert revised.previous_id == original.id
    assert revised.content_hash != original.content_hash
    assert latest.id == revised.id
    # the fact really moved, and both versions remain independently readable
    assert revised.payload["facts"][0]["value"]["raw_value"] == "1100"
    assert old_row.payload["facts"][0]["value"]["raw_value"] == "1000"
    assert old_row.content_hash == original.content_hash
    assert original_artifact.read_bytes() == original_bytes
    # the pre-revision acquisition still resolves to the old version
    all_acquisitions = await acquisition_rows()
    first_again = next(row for row in all_acquisitions if row.id == first_acquisition.id)
    assert first_again.dataset_links == [
        {"id": original.id, "revision": 1, "content_hash": original.content_hash}
    ]

    # Negative: an identical body re-fetched later (new fetched_at) adds nothing.
    job, third_run = await _claim(_licensed_payload(listing_id, "hk206-rev-c", (HK_SOURCE,)))
    assert await handler.execute(job, _Ctx()) == "completed"
    rows = await dataset_rows()
    assert len(rows) == 2
    third = await _run_row(third_run)
    assert third.metadata_json["saved"] == [
        {
            "source": HK_SOURCE,
            "dataset_id": revised.id,
            "created": False,
            "content_hash": revised.content_hash,
        }
    ]
    assert sorted(row.revision for row in rows) == [1, 2]  # no third revision appeared


# --- drill: offline_replay --------------------------------------------------


async def test_relocated_archive_replays_offline_with_identical_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drill: offline_replay (simulation).

    Database and original-response packages are copied to a new directory and
    the originals deleted; with sockets and HTTP blocked, replaying the
    acquisition reproduces exactly the stored version identity and facts
    without writing anything new. Corrupted and missing archive bodies are
    reported as errors instead of being worked around.
    """

    def no_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay must never open a socket")

    async def forbid_get(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline replay must never issue an HTTP request")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(httpx.AsyncClient, "get", forbid_get)
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)

    original_root = tmp_path / "original"
    original_root.mkdir()
    original_db = original_root / "research.db"
    original_data = original_root / "data"
    body = sim_body(3000)
    fetcher = ScriptedFetcher(
        script={
            HK_REQUEST_NAME: [
                RawPayload(
                    name=HK_REQUEST_NAME,
                    content=body,
                    media_type="application/json",
                    fetched_at=datetime(2026, 10, 17, 8, 0, tzinfo=UTC),
                    source_url=f"https://example.invalid/hk206/{HK_REQUEST_NAME}",
                    http_status=200,
                )
            ]
        }
    )
    handler = MaterialCollectionHandler(
        settings=_settings(original_data),
        fetcher=fetcher,
        adapters={HK_SOURCE: SyntheticAnnualAdapter(source=HK_SOURCE)},
        approvals=hsbc_two_source_approvals(),
    )
    async with engine_lifespan(f"sqlite+aiosqlite:///{original_db}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            await _seed(session)
            listing_id = await _listing_id(session, "HKEX", "00005")
        job, run_id = await _claim(
            _licensed_payload(listing_id, "hk206-offline-replay", (HK_SOURCE,))
        )
        assert await handler.execute(job, _Ctx()) == "completed"
        assert (await _run_row(run_id)).status == "success"
        before_rows = await dataset_rows()
        (before_acquisition,) = await acquisition_rows()
        before_signature = dataset_signature(before_rows)
    assert len(before_rows) == 1
    assert before_acquisition.dataset_links

    # Relocate database + original-response packages, then delete the originals.
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    copied_db = relocated / "research.db"
    copied_data = relocated / "data"
    copy_sqlite(original_db, copied_db)
    shutil.copytree(original_data, copied_data)
    shutil.rmtree(original_root)

    identities = (_identity(HSBC),)
    replay_adapters = {HK_SOURCE: SyntheticAnnualAdapter(source=HK_SOURCE)}
    async with engine_lifespan(f"sqlite+aiosqlite:///{copied_db}"):
        async with session_scope() as session:
            report = await replay_acquisition(
                session,
                acquisition_id=before_acquisition.id,
                data_dir=copied_data,
                identities=identities,
                adapters=replay_adapters,
            )
        assert report.problems == ()
        assert report.skipped == ()
        assert [(item.id, item.revision, item.content_hash) for item in report.saved] == [
            (row.id, row.revision, row.content_hash) for row in before_rows
        ]
        after_rows = await dataset_rows()
        assert dataset_signature(after_rows) == before_signature
        after_acquisitions = await acquisition_rows()
        assert [row.id for row in after_acquisitions] == [
            before_acquisition.id
        ]  # the replay reused the observation, it added none

        # Negative: a corrupted body is reported, never parsed around.
        verified = verify_capture(
            copied_data,
            before_acquisition.archive_ref,
            expected_sha256=before_acquisition.manifest_sha256,
        )
        (response,) = verified.responses
        digest = hashlib.sha256(response.content).hexdigest()
        body_ref = (
            Path("research-material/archive") / HK_SOURCE / "bodies" / digest[:2] / f"{digest}.bin"
        )
        corrupt_root = tmp_path / "neg-corrupt"
        shutil.copytree(copied_data, corrupt_root)
        (corrupt_root / body_ref).write_bytes(response.content + b"corruption")
        async with session_scope() as session:
            with pytest.raises(ReplayError, match="integrity"):
                await replay_acquisition(
                    session,
                    acquisition_id=before_acquisition.id,
                    data_dir=corrupt_root,
                    identities=identities,
                    adapters=replay_adapters,
                )

        # Negative: a missing body (a lost original package) is reported too.
        missing_root = tmp_path / "neg-missing"
        shutil.copytree(copied_data, missing_root)
        (missing_root / body_ref).unlink()
        async with session_scope() as session:
            with pytest.raises(ReplayError, match="unreadable"):
                await replay_acquisition(
                    session,
                    acquisition_id=before_acquisition.id,
                    data_dir=missing_root,
                    identities=identities,
                    adapters=replay_adapters,
                )

        # The rejected replays wrote nothing.
        assert dataset_signature(await dataset_rows()) == before_signature


# --- drill: probe_stop_withdrawal -------------------------------------------


def _assert_probe_artifacts_are_terminal(work_dir: Path, *, stop_file: bool = False) -> None:
    """A probe work dir holds only its report and content-addressed archives.

    There is no checkpoint, resume or state file anywhere: a probe cannot be
    continued, only re-run.
    """

    assert (work_dir / "report.json").is_file()
    allowed_roots = ("report.json", "STOP") if stop_file else ("report.json",)
    for path in sorted(work_dir.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(work_dir).as_posix()
        lowered = rel.lower()
        assert "checkpoint" not in lowered, rel
        assert "resume" not in lowered, rel
        if rel in allowed_roots:
            continue
        assert rel.startswith("research-material/archive/"), rel
        assert ("/manifests/" in rel and rel.endswith(".json")) or (
            "/bodies/" in rel and rel.endswith(".bin")
        ), rel


async def _run_probe(work_dir: Path, pilot_path: Path, identity: object, fetcher: ProbeStubFetcher):
    return await asyncio.to_thread(
        source_probe.run_source_probe,
        source=SOURCE_EASTMONEY_HK,
        work_dir=work_dir,
        user_agent=PROBE_UA,
        allow_network=True,
        periods=probe_fixtures._hk_periods(),
        dependencies=source_probe.ProbeDependencies(
            approvals=probe_licensing_approval(identity),  # type: ignore[arg-type]
            pilot_list_path=pilot_path,
            fetcher_factory=lambda _user_agent: fetcher,
        ),
    )


async def _app_material_state(db_session) -> tuple[int, int]:
    runs = await db_session.scalar(select(func.count()).select_from(PipelineRun))
    datasets = await db_session.scalar(select(func.count()).select_from(ResearchDataset))
    return int(runs or 0), int(datasets or 0)


async def test_probe_runs_are_one_shot_and_leave_no_pipeline_state(
    db_session, tmp_path: Path
) -> None:
    """Drill: probe_stop_withdrawal / one-shot boundary (simulation).

    A probe with an injected fetcher factory is a ``simulation`` run: it never
    creates a pipeline run or a dataset version, leaves no checkpoint files,
    refuses an existing work directory, and a second run is a fully
    independent new probe that re-issues every request — the probe has no
    restart_resume semantics at all.
    """

    pilot_path, identity = one_hk_pilot_list(tmp_path)
    work_dir = tmp_path / "probe-run-1"
    fetcher = ProbeStubFetcher(user_agent=PROBE_UA, identity=identity)
    report = await _run_probe(work_dir, pilot_path, identity, fetcher)

    assert report["mode"] == "simulation"
    assert report["status"] == "passed"
    assert fetcher.calls == ["income", "balance", "cashflow"]
    _assert_probe_artifacts_are_terminal(work_dir)
    assert await _app_material_state(db_session) == (0, 0)  # no run, no version

    # No resume: an existing work directory is refused outright.
    second_fetcher = ProbeStubFetcher(user_agent=PROBE_UA, identity=identity)
    with pytest.raises(FileExistsError):
        await _run_probe(work_dir, pilot_path, identity, second_fetcher)

    # A second run is a brand-new probe: every request is issued again.
    second_dir = tmp_path / "probe-run-2"
    report2 = await _run_probe(second_dir, pilot_path, identity, second_fetcher)
    assert report2["mode"] == "simulation"
    assert report2["status"] == "passed"
    assert second_fetcher.calls == ["income", "balance", "cashflow"]
    _assert_probe_artifacts_are_terminal(second_dir)
    assert await _app_material_state(db_session) == (0, 0)

    # Regression guard: the module exposes no checkpoint/resume API.
    assert [
        name
        for name in dir(source_probe)
        if "checkpoint" in name.lower() or "resume" in name.lower()
    ] == []


async def test_probe_stop_file_halts_requests_without_resume_trace(
    db_session, tmp_path: Path
) -> None:
    """Drill: probe_stop_withdrawal (simulation of the STOP signal).

    Dropping the probe's STOP file while it waits between requests stops every
    further send; the received responses stay archived as incomplete evidence,
    nothing enters the application database, and no state is left behind that
    could continue the run.
    """

    pilot_path, identity = one_hk_pilot_list(tmp_path)
    work_dir = tmp_path / "probe-stop"
    fetcher = ProbeStubFetcher(user_agent=PROBE_UA, identity=identity)

    def drop_stop_after_first_request() -> None:
        if fetcher.calls:
            (work_dir / source_probe.STOP_FILENAME).touch()

    fetcher.after_wait = drop_stop_after_first_request
    report = await _run_probe(work_dir, pilot_path, identity, fetcher)

    assert report["mode"] == "simulation"
    assert report["status"] == "failed"
    assert "stop_signal_after_limiter_wait:source_stopped" in report["issues"]
    assert fetcher.calls == ["income"]  # nothing after the STOP was sent
    assert report["requests_total"] == 1
    assert report["requests_succeeded"] == 1
    assert (work_dir / source_probe.STOP_FILENAME).is_file()
    _assert_probe_artifacts_are_terminal(work_dir, stop_file=True)
    assert await _app_material_state(db_session) == (0, 0)
