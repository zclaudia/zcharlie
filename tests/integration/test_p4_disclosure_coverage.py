"""AF-P4-07 source-coverage proof through the durable poll producer."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import SourceResponse
from investment_agent.evidence.disclosure_pages import DisclosurePageService
from investment_agent.investor.common import ObjectRef
from investment_agent.jobs.base import ClaimedJob
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.storage.repositories.supplemental_updates import (
    SupplementalUpdatesRepository,
    load_poll_window_proof,
)
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.coverage import build_update_source_binding
from investment_agent.supplemental_rules.disclosure_authorization import (
    apply_authorization_command,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    DisclosurePollRequest,
    admit_disclosure_poll,
)
from tests.support.p4_disclosures import FIXTURE_PDF, fixture_protocol, fixture_source_response


class _InlineContext:
    worker_id = "p4-coverage-test"

    async def run_with_heartbeat(self, _lease, work):
        return await work()


class _ControlledHTTP:
    """Test-only transport; the durable worker and receipt producer are real."""

    def __init__(
        self, *, mode: str = "empty", published_at: datetime | None = None,
        drift_on_verification: bool = False,
    ) -> None:
        self.mode = mode
        self.query_count = 0
        self.published_at = published_at or datetime.now(UTC) - timedelta(minutes=10)
        self.drift_on_verification = drift_on_verification
        self.native_id: str | None = None
        self.pdf_body = FIXTURE_PDF

    async def request_hop(self, url, purpose, *, body=None, max_bytes, timeout_seconds=15):
        del timeout_seconds
        status, headers, content = fixture_source_response(
            purpose,
            body or b"",
            published_at=self.published_at,
            mode=self.mode,
        )
        if purpose == "original":
            content = self.pdf_body
        if purpose == "query":
            self.query_count += 1
            import json

            material = json.loads(content)
            rows = material.get("announcements", [])
            for row in rows:
                old_id = row.get("announcementId")
                if self.native_id is not None:
                    row["announcementId"] = self.native_id
                    if isinstance(row.get("adjunctUrl"), str) and isinstance(old_id, str):
                        row["adjunctUrl"] = row["adjunctUrl"].replace(old_id, self.native_id)
            if self.drift_on_verification and self.query_count == 2 and rows:
                old_id = rows[0].get("announcementId")
                rows[0]["announcementId"] = "99999999"
                if isinstance(rows[0].get("adjunctUrl"), str) and isinstance(old_id, str):
                    rows[0]["adjunctUrl"] = rows[0]["adjunctUrl"].replace(old_id, "99999999")
            content = json.dumps(material, ensure_ascii=False).encode()
        if self.mode == "late_drift" and purpose == "query" and self.query_count == 3:
            import json

            material = json.loads(content)
            material["announcements"][0]["announcementId"] = "99999999"
            content = json.dumps(material).encode()
        if len(content) > max_bytes:
            raise AssertionError("fixture exceeded its declared response bound")
        return SourceResponse(status, headers, content, url)


class _AdvancingClock:
    def __init__(self, initial: datetime) -> None:
        self.current = initial

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=6)
        return self.current


@pytest_asyncio.fixture
async def p4_coverage_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'p4-coverage.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "p4-coverage-test")
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        api_key="p4-coverage-test",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True,
        disclosure_originals_enabled=True,
    )


async def _claim_next(run_id: int | None = None) -> ClaimedJob | None:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).claim_next(
            worker_id="p4-coverage-test",
            kind=DISCLOSURE_POLL_KIND,
            run_id=run_id,
        )
        if row is None:
            return None
        return ClaimedJob(
            run_id=row.id,
            kind=row.kind,
            lease=RunLease(row.id, "p4-coverage-test", row.attempt_count),
            request_json=dict(row.request_json),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_documents", "expected_state"),
    [
        ("empty", 0, "completed"),
        ("normal", 1, "completed"),
        ("two_pages", 21, "completed"),
        ("late_drift", 0, "source_page_changed_between_passes"),
        ("replacement_drift", 1, "source_page_changed_between_passes"),
        ("partial_window", 1, "completed"),
    ],
)
async def test_poll_proof_comes_from_durable_worker_and_fixture_is_test_gated(
    p4_coverage_db,
    tmp_path: Path,
    mode: str,
    expected_documents: int,
    expected_state: str,
    monkeypatch: pytest.MonkeyPatch,
):
    # Explicitly opt this controlled test session into fixture proofs. The
    # production worker/API never receives this session.info flag.
    import investment_agent.supplemental_rules.disclosure_updates as updates_module

    original_session_scope = updates_module.session_scope

    @asynccontextmanager
    async def fixture_proof_sessions():
        async with original_session_scope() as session:
            session.info["allow_disclosure_fixture_proofs"] = True
            yield session

    monkeypatch.setattr(updates_module, "session_scope", fixture_proof_sessions)
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=1)
    window_start = now - timedelta(days=1)
    grant_history_start = now - timedelta(days=10) if mode == "partial_window" else window_start
    contract_hash = canonical_hash({"fixture": "poll-empty-coverage"})
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 empty poll fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        body = BackgroundDisclosureAuthorizationBody(
            contract_hash=contract_hash,
            listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=grant_history_start,
            history_end=now + timedelta(days=1),
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=now + timedelta(days=5),
            max_staleness_seconds=86400,
            per_poll_query_http=4,
            per_poll_fetch=20,
            per_poll_http=32,
            per_poll_response_bytes=64 * 1024 * 1024,
            grant_daily_http=50,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        command = BackgroundDisclosureAuthorizationCommand(
            action="create",
            operation_id=uuid4(),
            authorization_id=uuid4(),
            expected_revision=0,
            owner_daily_http=50,
            owner_daily_response_bytes=128 * 1024 * 1024,
            owner_min_request_interval_seconds=5,
            expected_owner_policy_revision=0,
            update_owner_policy=True,
            body=body,
        )
        grant = (
            await apply_authorization_command(
                session,
                command,
                actor="test",
                now=now,
            )
        ).authorization
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=window_start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
        )
        listing_id = listing.id
        run_id = admitted["run_id"]

    controlled_http = _ControlledHTTP(
        mode="normal" if mode == "replacement_drift" else mode,
        published_at=now - timedelta(hours=1),
        drift_on_verification=mode == "replacement_drift",
    )
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=controlled_http)
    clock = _AdvancingClock(datetime.now(UTC))
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=service,
        clock=clock,
    )

    async def drive_run(start_run_id: int) -> None:
        next_run_id: int | None = start_run_id

        for _ in range(40):
            job = await _claim_next(next_run_id)
            if job is None:
                break
            next_run_id = None
            async with session_scope() as session:
                root = await SupplementalUpdatesRepository(session).get_change(
                    "local_owner",
                    int(job.request_json.get("poll_sequence", 0)),
                )
                assert root is not None, job.request_json
                assert str(root.id) == str(job.request_json.get("poll_id")), (root.id, job.request_json)
                assert root.payload_hash == job.request_json.get("poll_hash"), (
                    root.payload_hash,
                    job.request_json,
                )
            result = await handler.execute(job, _InlineContext())
            assert result in {"completed", "partial"}

    await drive_run(run_id)

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        run_row = await session.get(PipelineRun, run_id)
        assert source is not None
        assert source.payload["poll_state"] == expected_state, (
            source.payload,
            run_row.status if run_row else None,
            run_row.error_summary if run_row else None,
            controlled_http.query_count,
        )
        if expected_state != "completed":
            assert (
                await load_poll_window_proof(
                    session,
                    owner="local_owner",
                    listing_id=listing_id,
                    context="company_research",
                    cutoff=clock.current + timedelta(seconds=1),
                    expected_source_state=source,
                )
                is None
            )
            if mode != "replacement_drift":
                return
            # Fail after the candidate original/event commits but before the
            # double-pass seal. Replacement must prove the inherited semantic
            # increment from the immutable checkpoint snapshot.
            assert source.payload.get("semantic_generation") == 1
            failed_poll_id = source.payload.get("active_poll_id")
            assert isinstance(failed_poll_id, str)
            controlled_http.drift_on_verification = False
            replacement_now = now + timedelta(hours=7)
            async with session_scope() as replacement_session:
                current_state = await SupplementalUpdatesRepository(replacement_session).get_source(
                    "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
                )
                assert current_state is not None
                replacement = await admit_disclosure_poll(
                    replacement_session,
                    DisclosurePollRequest(
                        operation_id=uuid4(), authorization_id=grant.authorization_id,
                        authorization_revision=grant.revision,
                        authorization_hash=grant.authorization_hash, listing_ref=listing_ref,
                        window_start=window_start, window_end=replacement_now,
                        expected_source_revision=current_state.revision,
                        replace_poll_id=UUID(failed_poll_id),
                        replace_reason="resume candidate committed before poll seal",
                    ),
                    settings=settings, now=replacement_now,
                )
            replacement_clock = _AdvancingClock(replacement_now)
            handler = DisclosurePollingHandler.for_tests(
                settings=settings, page_service=service, clock=replacement_clock,
            )
            await drive_run(replacement["run_id"])
            async with session_scope() as recovered:
                recovered_state = await SupplementalUpdatesRepository(recovered).get_source(
                    "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
                )
                assert recovered_state is not None
                assert recovered_state.payload.get("poll_state") == "completed"
                assert recovered_state.payload.get("semantic_generation") == 1
                recovered.info["allow_disclosure_fixture_proofs"] = True
                recovered_proof = await load_poll_window_proof(
                    recovered, owner="local_owner", listing_id=listing_id,
                    context="company_research", cutoff=replacement_clock.current + timedelta(seconds=1),
                    expected_source_state=recovered_state,
                )
                assert recovered_proof is not None
                assert recovered_proof["semantic_generation"] == 1
            return
        proof_cutoff = clock.current + timedelta(seconds=1)
        assert await load_poll_window_proof(
            session,
            owner="local_owner",
            listing_id=listing_id,
            context="company_research",
            cutoff=clock.current - timedelta(seconds=1),
        ) is None
        listing_ref = ObjectRef(kind="listing", id=listing_id, revision=1)
        final_data = await load_poll_window_proof(
            session,
            owner="local_owner",
            listing_id=listing_id,
            context="company_research",
            cutoff=proof_cutoff,
            expected_source_state=None if mode == "partial_window" else source,
        )
        assert final_data is not None
        assert final_data["document_count"] == expected_documents
        assert final_data["protocol_profile"] == "fixture"
        if mode == "partial_window":
            assert source.coverage_state == "partial"
            assert all(final_data[name] is None for name in (
                "continuous_index_watermark", "continuous_original_watermark",
                "continuous_complete_watermark",
            ))
            assert await load_poll_window_proof(
                session, owner="local_owner", listing_id=listing_id,
                context="company_research", cutoff=proof_cutoff,
                expected_source_state=source,
            ) is None
            assert await build_update_source_binding(
                session, owner="local_owner", listing_ref=listing_ref,
                observed_at=proof_cutoff, knowledge_cutoff=proof_cutoff,
            ) is None
            second_now = now + timedelta(hours=7)
            await session.commit()
            async with session_scope() as admission_session:
                current_state = await SupplementalUpdatesRepository(admission_session).get_source(
                    "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
                )
                assert current_state is not None
                backfill = await admit_disclosure_poll(
                    admission_session,
                    DisclosurePollRequest(
                        operation_id=uuid4(), authorization_id=grant.authorization_id,
                        authorization_revision=grant.revision,
                        authorization_hash=grant.authorization_hash, listing_ref=listing_ref,
                        window_start=grant_history_start, window_end=second_now,
                        expected_source_revision=current_state.revision,
                    ),
                    settings=settings, now=second_now,
                )
            backfill_clock = _AdvancingClock(second_now)
            handler = DisclosurePollingHandler.for_tests(
                settings=settings, page_service=service, clock=backfill_clock,
            )
            await drive_run(backfill["run_id"])
            async with session_scope() as backfilled:
                backfilled_state = await SupplementalUpdatesRepository(backfilled).get_source(
                    "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
                )
                assert backfilled_state is not None
                assert backfilled_state.coverage_state == "complete"
                backfilled.info["allow_disclosure_fixture_proofs"] = True
                backfilled_cutoff = backfill_clock.current + timedelta(seconds=1)
                backfilled_proof = await load_poll_window_proof(
                    backfilled, owner="local_owner", listing_id=listing_id,
                    context="company_research", cutoff=backfilled_cutoff,
                    expected_source_state=backfilled_state,
                )
                assert backfilled_proof is not None
                assert all(
                    datetime.fromisoformat(backfilled_proof[name].replace("Z", "+00:00"))
                    == second_now
                    for name in (
                        "continuous_index_watermark", "continuous_original_watermark",
                        "continuous_complete_watermark",
                    )
                )
                backfilled_binding = await build_update_source_binding(
                    backfilled, owner="local_owner", listing_ref=listing_ref,
                    observed_at=backfilled_cutoff, knowledge_cutoff=backfilled_cutoff,
                )
                assert backfilled_binding is not None
            return
        binding_without_fixture_consent = await build_update_source_binding(
            session,
            owner="local_owner",
            listing_ref=listing_ref,
            observed_at=proof_cutoff,
            knowledge_cutoff=proof_cutoff,
        )
        assert binding_without_fixture_consent is None
        session.info["allow_disclosure_fixture_proofs"] = True
        binding = await build_update_source_binding(
            session,
            owner="local_owner",
            listing_ref=listing_ref,
            observed_at=proof_cutoff,
            knowledge_cutoff=proof_cutoff,
        )
        assert binding is not None
        assert binding.sources[0].document_count == expected_documents
        first_final_ref = final_data["final_ref"]
        first_epoch = final_data["history_epoch"]
        first_generation = final_data["semantic_generation"]
        # A syntactically valid but absent predecessor receipt must not be
        # accepted as the immutable basis for a final proof.
        repo = SupplementalUpdatesRepository(session)
        first_final_row = await repo.get_change(
            "local_owner", first_final_ref["sequence"],
        )
        assert first_final_row is not None
        bad_previous_payload = dict(first_final_row.payload)
        bad_previous_payload["previous_final_ref"] = {
            "sequence": 999999,
            "id": str(uuid4()),
            "payload_hash": "0" * 64,
        }
        bad_previous_committed_at = first_final_row.committed_at + timedelta(minutes=1)
        bad_previous_payload["committed_at"] = bad_previous_committed_at.isoformat()
        bad_previous_final = await repo.append_change(
            "local_owner",
            change_key=f"test:poll-final-bad-previous:{first_final_row.id}",
            kind="source_page",
            parent_sequence=first_final_row.parent_sequence,
            listing_id=listing_id,
            context="company_research",
            payload=bad_previous_payload,
            acquired_at=first_final_row.acquired_at,
            available_from=first_final_row.available_from,
            committed_at=bad_previous_committed_at,
        )
        assert await load_poll_window_proof(
            session,
            owner="local_owner",
            listing_id=listing_id,
            context="company_research",
            cutoff=bad_previous_committed_at + timedelta(seconds=1),
            expected_source_state=source,
            expected_final_ref={
                "sequence": bad_previous_final.sequence,
                "id": str(bad_previous_final.id),
                "payload_hash": bad_previous_final.payload_hash,
            },
        ) is None
        # The worker run and source receipts remain durable after closing and
        # reopening the SQLite session; no identity-map state supplies proof.
        assert run_id is not None

    if expected_state == "completed" and expected_documents <= 1:
        # Same-fact health refresh at a later observation time must not change
        # semantic identity or invalidate the old V3 source proof.
        second_now = now + timedelta(hours=7)
        async with session_scope() as admission_session:
            current_state = await SupplementalUpdatesRepository(admission_session).get_source(
                "local_owner",
                "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert current_state is not None
            second_admission = await admit_disclosure_poll(
                admission_session,
                DisclosurePollRequest(
                    operation_id=uuid4(),
                    authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash,
                    listing_ref=listing_ref,
                    window_start=window_start,
                    window_end=second_now,
                    expected_source_revision=current_state.revision,
                ),
                settings=settings,
                now=second_now,
            )
            second_run_id = second_admission["run_id"]
        second_clock = _AdvancingClock(second_now)
        second_handler = DisclosurePollingHandler.for_tests(
            settings=settings,
            page_service=service,
            clock=second_clock,
        )
        handler = second_handler
        await drive_run(second_run_id)
        proof_cutoff = second_clock.current + timedelta(seconds=1)
        async with session_scope() as refreshed:
            refreshed_repo = SupplementalUpdatesRepository(refreshed)
            refreshed_state = await refreshed_repo.get_source(
                "local_owner",
                "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert refreshed_state is not None
            latest = await load_poll_window_proof(
                refreshed,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
                expected_source_state=refreshed_state,
            )
            historical = await load_poll_window_proof(
                refreshed,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
                expected_source_state=refreshed_state,
                expected_final_ref=first_final_ref,
            )
            assert latest is not None and historical is not None
            assert historical["final_ref"] == first_final_ref
            assert latest["history_epoch"] == historical["history_epoch"] == first_epoch
            assert (
                latest["semantic_generation"]
                == historical["semantic_generation"]
                == first_generation
            )
            refreshed.info["allow_disclosure_fixture_proofs"] = True
            refreshed_binding = await build_update_source_binding(
                refreshed,
                owner="local_owner",
                listing_ref=listing_ref,
                observed_at=proof_cutoff,
                knowledge_cutoff=proof_cutoff,
            )
            assert refreshed_binding is not None
            assert refreshed_binding.sources[0].semantic_generation == first_generation

            # A source-state claim cannot upgrade a proof from a different
            # semantic generation or history epoch, even when receipt rows
            # and the old final remain intact.
            for field, value in (
                ("semantic_generation", first_generation + 1),
                ("history_epoch", str(uuid4())),
            ):
                changed_payload = dict(refreshed_state.payload)
                changed_payload[field] = value
                changed_state = refreshed_state.model_copy(
                    update={
                        "revision": refreshed_state.revision + 1,
                        "payload": changed_payload,
                    }
                )
                await refreshed_repo.save_source(
                    changed_state,
                    expected_revision=refreshed_state.revision,
                )
                assert await load_poll_window_proof(
                    refreshed,
                    owner="local_owner",
                    listing_id=listing_id,
                    context="company_research",
                    cutoff=proof_cutoff,
                    expected_source_state=changed_state,
                ) is None
                assert await build_update_source_binding(
                    refreshed,
                    owner="local_owner",
                    listing_ref=listing_ref,
                    observed_at=proof_cutoff,
                    knowledge_cutoff=proof_cutoff,
                ) is None
                refreshed_state = changed_state

            # Restore the legitimate projection after generation/epoch
            # tampering so subsequent worker scenarios start from a valid
            # source state.
            restored_payload = dict(refreshed_state.payload)
            restored_payload["semantic_generation"] = first_generation
            restored_payload["history_epoch"] = first_epoch
            restored = refreshed_state.model_copy(
                update={
                    "revision": refreshed_state.revision + 1,
                    "payload": restored_payload,
                }
            )
            await refreshed_repo.save_source(
                restored, expected_revision=refreshed_state.revision,
            )

    if mode == "normal":
        # A new native document must be able to advance semantic generation
        # while preserving epoch continuity; the old final remains historical
        # evidence but cannot be used as the current binding.
        third_now = now + timedelta(hours=14)
        controlled_http.native_id = "10000002"
        controlled_http.pdf_body = b"%PDF-1.4\nNew native document fixture.\n%%EOF"
        async with session_scope() as admission_session:
            repo = SupplementalUpdatesRepository(admission_session)
            current_state = await repo.get_source(
                "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
            )
            assert current_state is not None
            admitted_new_fact = await admit_disclosure_poll(
                admission_session,
                DisclosurePollRequest(
                    operation_id=uuid4(), authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash, listing_ref=listing_ref,
                    window_start=window_start, window_end=third_now,
                    expected_source_revision=current_state.revision,
                ),
                settings=settings, now=third_now,
            )
        third_clock = _AdvancingClock(third_now)
        handler = DisclosurePollingHandler.for_tests(
            settings=settings, page_service=service, clock=third_clock,
        )
        await drive_run(admitted_new_fact["run_id"])
        async with session_scope() as refreshed_new_fact:
            repo = SupplementalUpdatesRepository(refreshed_new_fact)
            current_state = await repo.get_source(
                "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research"
            )
            assert current_state is not None
            assert current_state.payload.get("poll_state") == "completed"
            assert current_state.payload.get("semantic_generation") == first_generation + 1
            refreshed_new_fact.info["allow_disclosure_fixture_proofs"] = True
            cutoff = third_clock.current + timedelta(seconds=1)
            current_proof = await load_poll_window_proof(
                refreshed_new_fact, owner="local_owner", listing_id=listing_id,
                context="company_research", cutoff=cutoff,
                expected_source_state=current_state,
            )
            assert current_proof is not None
            assert current_proof["semantic_generation"] == first_generation + 1
            assert current_proof["history_epoch"] == first_epoch
            assert await load_poll_window_proof(
                refreshed_new_fact, owner="local_owner", listing_id=listing_id,
                context="company_research", cutoff=cutoff,
                expected_source_state=current_state, expected_final_ref=first_final_ref,
            ) is None
            current_binding = await build_update_source_binding(
                refreshed_new_fact, owner="local_owner", listing_ref=listing_ref,
                observed_at=cutoff, knowledge_cutoff=cutoff,
            )
            assert current_binding is not None
            assert current_binding.sources[0].semantic_generation == first_generation + 1

    if mode == "empty":
        # A same-basis poll cannot carry a forged mutable frontier into a new
        # final. The writer must compare it with the preceding immutable final.
        tampered_poll_at = second_now + timedelta(hours=7)
        async with session_scope() as tamper_session:
            repo = SupplementalUpdatesRepository(tamper_session)
            current_state = await repo.get_source(
                "local_owner", "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert current_state is not None
            tampered_payload = dict(current_state.payload)
            tampered_payload["continuous_complete_watermark"] = (
                (second_now + timedelta(days=1)).isoformat()
            )
            tampered_state = current_state.model_copy(update={
                "revision": current_state.revision + 1,
                "payload": tampered_payload,
            })
            await repo.save_source(tampered_state, expected_revision=current_state.revision)
            tampered_admission = await admit_disclosure_poll(
                tamper_session,
                DisclosurePollRequest(
                    operation_id=uuid4(),
                    authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash,
                    listing_ref=listing_ref,
                    window_start=window_start,
                    window_end=second_now,
                    expected_source_revision=tampered_state.revision,
                ),
                settings=settings,
                now=tampered_poll_at,
            )
        tampered_handler = DisclosurePollingHandler.for_tests(
            settings=settings,
            page_service=DisclosurePageService.for_tests(
                protocol=fixture_protocol(),
                http=_ControlledHTTP(mode="empty", published_at=second_now),
            ),
            clock=_AdvancingClock(tampered_poll_at),
        )
        handler = tampered_handler
        await drive_run(tampered_admission["run_id"])
        async with session_scope() as tamper_session:
            repo = SupplementalUpdatesRepository(tamper_session)
            failed_state = await repo.get_source(
                "local_owner", "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert failed_state is not None
            assert failed_state.payload.get("poll_state") == "previous_final_invalid"
            assert await repo.get_change_by_key(
                "local_owner", f"p4:disclosure-final:{tampered_admission['poll_id']}"
            ) is None
            restored_state = failed_state.model_copy(update={
                "revision": failed_state.revision + 1,
                "coverage_state": restored.coverage_state,
                "complete_watermark": restored.complete_watermark,
                "last_success_at": restored.last_success_at,
                "last_attempt_at": restored.last_attempt_at,
                "success_sequence": restored.success_sequence,
                "success_receipt_hash": restored.success_receipt_hash,
                "failure_code": None,
                "retry_at": None,
                "payload": dict(restored.payload),
            })
            await repo.save_source(restored_state, expected_revision=failed_state.revision)

        # Produce a real failed poll, then an explicitly authorized narrow
        # replacement. The replacement has a final receipt, but cannot resolve
        # the old publication window because it starts too late.
        failed_admitted_at = tampered_poll_at + timedelta(hours=7)
        async with session_scope() as admission_session:
            current_state = await SupplementalUpdatesRepository(admission_session).get_source(
                "local_owner", "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert current_state is not None
            failed_admission = await admit_disclosure_poll(
                admission_session,
                DisclosurePollRequest(
                    operation_id=uuid4(),
                    authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash,
                    listing_ref=listing_ref,
                    window_start=window_start,
                    window_end=second_now,
                    expected_source_revision=current_state.revision,
                ),
                settings=settings,
                now=failed_admitted_at,
            )
        failed_service = DisclosurePageService.for_tests(
            protocol=fixture_protocol(),
            http=_ControlledHTTP(mode="rate_limited", published_at=second_now),
        )
        failed_handler = DisclosurePollingHandler.for_tests(
            settings=settings,
            page_service=failed_service,
            clock=_AdvancingClock(failed_admitted_at),
        )
        handler = failed_handler
        await drive_run(failed_admission["run_id"])
        failed_poll_id = failed_admission["poll_id"]
        replacement_admitted_at = failed_admitted_at + timedelta(hours=7)
        replacement_start = second_now - timedelta(hours=12)
        async with session_scope() as admission_session:
            current_state = await SupplementalUpdatesRepository(admission_session).get_source(
                "local_owner", "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert current_state is not None
            replacement = await admit_disclosure_poll(
                admission_session,
                DisclosurePollRequest(
                    operation_id=uuid4(),
                    authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision,
                    authorization_hash=grant.authorization_hash,
                    listing_ref=listing_ref,
                    window_start=replacement_start,
                    window_end=second_now,
                    expected_source_revision=current_state.revision,
                    replace_poll_id=failed_poll_id,
                    replace_reason="coverage regression for unresolved predecessor",
                ),
                settings=settings,
                now=replacement_admitted_at,
            )
        replacement_service = DisclosurePageService.for_tests(
            protocol=fixture_protocol(),
            http=_ControlledHTTP(mode="empty", published_at=second_now),
        )
        replacement_clock = _AdvancingClock(replacement_admitted_at)
        handler = DisclosurePollingHandler.for_tests(
            settings=settings,
            page_service=replacement_service,
            clock=replacement_clock,
        )
        await drive_run(replacement["run_id"])
        proof_cutoff = replacement_clock.current + timedelta(seconds=1)
        async with session_scope() as tamper_session:
            repo = SupplementalUpdatesRepository(tamper_session)
            current_state = await repo.get_source(
                "local_owner", "cninfo_szse_v1",
                f"listing:{listing_id}:company_research",
            )
            assert current_state is not None
            assert current_state.coverage_state == "partial"
            immutable_pending_proof = await load_poll_window_proof(
                tamper_session,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
            )
            assert immutable_pending_proof is not None
            assert immutable_pending_proof["predecessor_resolution"] == "pending"
            immutable_pending_row = await repo.get_change(
                "local_owner", immutable_pending_proof["final_ref"]["sequence"],
            )
            assert immutable_pending_row is not None
            missing_previous_payload = dict(immutable_pending_row.payload)
            missing_previous_payload.pop("previous_final_ref", None)
            missing_previous_committed_at = (
                immutable_pending_row.committed_at + timedelta(minutes=1)
            )
            missing_previous_payload["committed_at"] = (
                missing_previous_committed_at.isoformat()
            )
            missing_previous_payload["observation_at"] = (
                missing_previous_committed_at.isoformat()
            )
            missing_previous_payload["last_success_at"] = (
                missing_previous_committed_at.isoformat()
            )
            missing_previous_final = await repo.append_change(
                "local_owner",
                change_key=f"test:poll-final-missing-previous:{immutable_pending_row.id}",
                kind="source_page",
                parent_sequence=immutable_pending_row.parent_sequence,
                listing_id=listing_id,
                context="company_research",
                payload=missing_previous_payload,
                acquired_at=immutable_pending_row.acquired_at,
                available_from=immutable_pending_row.available_from,
                committed_at=missing_previous_committed_at,
            )
            assert await load_poll_window_proof(
                tamper_session,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=missing_previous_committed_at + timedelta(seconds=1),
                expected_final_ref={
                    "sequence": missing_previous_final.sequence,
                    "id": str(missing_previous_final.id),
                    "payload_hash": missing_previous_final.payload_hash,
                },
            ) is None
            assert await load_poll_window_proof(
                tamper_session,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
                expected_source_state=current_state,
            ) is None

            # A mutable projection cannot hide that immutable pending marker,
            # even when its coverage state, gap flags and predecessor field
            # are forged to look complete.
            forged_payload = dict(current_state.payload)
            forged_payload.update({
                "pending_predecessor": None,
                "history_gap": False,
                "history_frontier": second_now.isoformat(),
                "continuous_index_watermark": second_now.isoformat(),
                "continuous_original_watermark": second_now.isoformat(),
                "continuous_complete_watermark": second_now.isoformat(),
            })
            forged_state = current_state.model_copy(update={
                "revision": current_state.revision + 1,
                "coverage_state": "complete",
                "complete_watermark": second_now,
                "payload": forged_payload,
            })
            await repo.save_source(forged_state, expected_revision=current_state.revision)
            tamper_session.info["allow_disclosure_fixture_proofs"] = True
            assert await load_poll_window_proof(
                tamper_session,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
                expected_source_state=forged_state,
            ) is None
            assert await build_update_source_binding(
                tamper_session,
                owner="local_owner",
                listing_ref=listing_ref,
                observed_at=proof_cutoff,
                knowledge_cutoff=proof_cutoff,
            ) is None

    async with session_scope() as fresh_session:
        proof = await load_poll_window_proof(
            fresh_session,
            owner="local_owner",
            listing_id=listing_id,
            context="company_research",
            cutoff=proof_cutoff,
        )
        assert proof is not None and proof["document_count"] == expected_documents
        content_hash = None
        if mode == "normal":
            proof_repo = SupplementalUpdatesRepository(fresh_session)
            final_row = await proof_repo.get_change(
                "local_owner",
                proof["final_ref"]["sequence"],
            )
            assert final_row is not None
            bridge = await proof_repo.get_change(
                "local_owner",
                proof["child_receipts"][0]["sequence"],
            )
            assert bridge is not None
            leaf_ref = bridge.payload["document_refs"][0]
            leaf = await proof_repo.get_change("local_owner", leaf_ref["sequence"])
            assert leaf is not None
            content_hash = leaf.payload["content_hash"]
            original = await fresh_session.get(
                DisclosureOriginal,
                str(leaf.payload["original_ref"]["id"]),
            )
            blob = await fresh_session.get(DisclosureBlob, content_hash)
            assert original is not None and original.content_hash == content_hash
            assert blob is not None and hashlib.sha256(blob.body).hexdigest() == content_hash
            assert final_row.payload_hash == proof["final_ref"]["payload_hash"]

    # Exercise SQLite's supported backup API, close the original engine, and
    # verify the bounded proof from the independent recovered database.
    source_db = tmp_path / "p4-coverage.sqlite"
    backup_db = tmp_path / "p4-coverage-backup.sqlite"
    with (
        sqlite3.connect(source_db) as source_connection,
        sqlite3.connect(backup_db) as backup_connection,
    ):
        source_connection.backup(backup_connection)
    await dispose_engine()
    init_engine(f"sqlite+aiosqlite:///{backup_db}")
    async with session_scope() as recovered:
        recovered_proof = await load_poll_window_proof(
            recovered,
            owner="local_owner",
            listing_id=listing_id,
            context="company_research",
            cutoff=proof_cutoff,
        )
        assert recovered_proof is not None
        assert recovered_proof["document_count"] == expected_documents
        if content_hash is not None:
            recovered_blob = await recovered.get(DisclosureBlob, content_hash)
            assert recovered_blob is not None
            assert hashlib.sha256(recovered_blob.body).hexdigest() == content_hash
    if content_hash is not None:
        # Missing raw bytes must make the proof unavailable after restore.
        await dispose_engine()
        with sqlite3.connect(backup_db) as backup_connection:
            backup_connection.execute(
                "DELETE FROM disclosure_blob WHERE content_hash = ?",
                (content_hash,),
            )
            backup_connection.commit()
        init_engine(f"sqlite+aiosqlite:///{backup_db}")
        async with session_scope() as damaged:
            missing_blob_proof = await load_poll_window_proof(
                damaged,
                owner="local_owner",
                listing_id=listing_id,
                context="company_research",
                cutoff=proof_cutoff,
            )
            assert missing_blob_proof is None
