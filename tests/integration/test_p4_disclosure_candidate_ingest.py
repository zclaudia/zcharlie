from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import QUERY
from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument
from investment_agent.investor.common import ObjectRef
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRegistryHead,
)
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_authorization import (
    apply_authorization_command,
    freeze_poll_quota,
    mark_http_hop_sent,
    reserve_http_hop,
    settle_http_hop_result,
)
from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
    ingest_disclosure_original,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
    DisclosureAttemptIdentity,
    authorization_scope_hash,
)


def _pdf(marker: str = "A") -> bytes:
    return f"%PDF-1.4\nControlled candidate-only fixture {marker}.\n%%EOF".encode()


@pytest.mark.asyncio
async def test_ingest_is_atomic_candidate_only_and_same_hash_does_not_revise_event(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    market = Market(code="SZSE", name="Shenzhen fixture", timezone="Asia/Shanghai", currency="CNY")
    db_session.add(market)
    company = Company(canonical_name="P4 disclosure candidate ingest fixture")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
    db_session.add(listing)
    await db_session.flush()
    listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)

    body = BackgroundDisclosureAuthorizationBody(
        contract_hash="a" * 64,
        listing_refs=(listing_ref,),
        doc_type_allowlist=("annual_report_full",),
        history_start=now - timedelta(days=4), history_end=now,
        min_poll_interval_seconds=21600, min_http_interval_seconds=5,
        expires_at=now + timedelta(days=4), max_staleness_seconds=86400,
        per_poll_query_http=2, per_poll_fetch=8, per_poll_http=10,
        per_poll_response_bytes=6 * 1024 * 1024,
        grant_daily_http=8, grant_daily_response_bytes=8 * 1024 * 1024,
        storage_bytes_cap=64 * 1024 * 1024,
    )
    command = BackgroundDisclosureAuthorizationCommand(
        action="create", operation_id=uuid4(), authorization_id=uuid4(), expected_revision=0,
        owner_daily_http=24, owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5, expected_owner_policy_revision=0,
        update_owner_policy=True, body=body,
    )
    grant = (await apply_authorization_command(db_session, command, actor="api", now=now)).authorization
    poll_id = uuid4()
    attempt_scope = authorization_scope_hash(
        grant.authorization_id, grant.revision, grant.authorization_hash,
        grant.contract_hash, listing_ref, grant.history_start, grant.history_end,
    )
    await freeze_poll_quota(
        db_session, poll_id=str(poll_id), authorization=grant,
        listing_ref=listing_ref, window_start=grant.history_start,
        window_end=grant.history_end, poll_http_cap=10, poll_query_http_cap=2,
        poll_fetch_cap=8, poll_response_bytes_cap=6 * 1024 * 1024, now=now,
    )
    settings = Settings(
        api_key="candidate-ingest-test",
        supplemental_rules_enabled=True, supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True, disclosure_originals_enabled=True,
    )
    run = PipelineRun(
        kind="supplemental_disclosure_poll", triggered_by="test", started_at=now,
        status="running", stage="fetch", attempt_count=1, worker_id="candidate-ingest-test",
        request_json={},
    )
    db_session.add(run)
    await db_session.flush()
    lease = RunLease(run_id=run.id, worker_id="candidate-ingest-test", attempt=1)
    job = ClaimedJob(run_id=run.id, kind=run.kind, lease=lease)
    updates = SupplementalUpdatesRepository(db_session)
    query_body = b'{"announcements":[]}'
    query_identity = DisclosureAttemptIdentity(
        poll_id=str(poll_id), logical_request_key="query:1", attempt_id=uuid4(),
        attempt_ordinal=1, hop_ordinal=0, hop_id="query:1:hop:0", request_kind="query",
        request_hash=canonical_hash({"query": 1}), authorization_id=grant.authorization_id,
        authorization_revision=grant.revision, authorization_hash=grant.authorization_hash,
        contract_hash=grant.contract_hash, scope_hash=authorization_scope_hash(
            grant.authorization_id, grant.revision, grant.authorization_hash,
            grant.contract_hash, listing_ref, grant.history_start, grant.history_end,
        ), listing_ref=listing_ref, window_start=grant.history_start,
        window_end=grant.history_end, poll_http_cap=10, poll_query_http_cap=2,
        poll_fetch_cap=8, poll_response_bytes_cap=6 * 1024 * 1024,
        url=QUERY, response_bytes_limit=1024 * 1024,
    )
    query_reserved = await reserve_http_hop(
        db_session, identity=query_identity, response_byte_limit=query_identity.response_bytes_limit,
        lease=lease, base_settings=settings, now=now,
    )
    query_sent = await mark_http_hop_sent(
        db_session, permit=query_reserved, lease=lease, base_settings=settings, now=now,
    )
    await settle_http_hop_result(
        db_session, permit=query_sent, lease=lease, response_bytes=len(query_body),
        result_code="http_200_json", response_hash=hashlib.sha256(query_body).hexdigest(),
        storage_bytes=len(query_body), now=now,
    )
    query_receipts = {}
    for phase in ("reserved", "sent", "result"):
        receipt = await updates.get_change_by_key(
            "local_owner", f"p4:attempt:{query_identity.attempt_id}:0:{phase}",
        )
        assert receipt is not None
        query_receipts[phase] = {
            "sequence": receipt.sequence, "id": str(receipt.id),
            "payload_hash": receipt.payload_hash,
        }
    query_refs = [query_receipts[key] for key in ("reserved", "sent", "result")]
    source_scope = canonical_hash({
        "source_id": "cninfo_szse_v1", "contract_version": "1",
        "contract_hash": grant.contract_hash,
        "scope_key": f"listing:{listing.id}:company_research", "listing_ref": listing_ref,
    })
    await updates.ensure_source(
        "local_owner", "cninfo_szse_v1", f"listing:{listing.id}:company_research",
        grant.contract_hash, contract_version="1", listing_id=listing.id,
        context="company_research",
        payload={
            "source_scope_hash": source_scope, "active_poll_id": str(poll_id),
            "active_step_run_id": run.id, "semantic_generation": 0,
            "poll_state": "running",
        },
    )
    poll_root = await updates.append_change(
        "local_owner", change_key=f"test:poll-root:{poll_id}", kind="source_attempt",
        receipt_id=poll_id,
        listing_id=listing.id, context="company_research",
        payload={
            "record_type": "poll_root_v1", "poll_id": str(poll_id),
            "authorization_id": str(grant.authorization_id),
            "authorization_revision": grant.revision,
            "authorization_hash": grant.authorization_hash,
            "contract_hash": grant.contract_hash,
            "source_scope_hash": source_scope,
            "attempt_scope_hash": attempt_scope,
            "listing_ref": listing_ref.model_dump(mode="json"),
            "window_start": grant.history_start.isoformat(),
            "window_end": grant.history_end.isoformat(),
        },
        acquired_at=now, available_from=now, committed_at=now,
    )
    assert str(poll_root.id) == str(poll_id)
    assert poll_root.payload["window_start"] == grant.history_start.isoformat()
    assert poll_root.payload["window_end"] == grant.history_end.isoformat()
    document = CNInfoDiscoveredDocument(
        native_announcement_id="10000001", ticker="300901", issuer_id="fixture_org",
        published_at=now - timedelta(days=1), title="年度报告全文（测试夹具）",
        locator="https://static.cninfo.com.cn/finalpage/2026-10-16/10000001.PDF", source_type="annual_full",
        published_raw_millis=int((now - timedelta(days=1)).timestamp() * 1000),
        stable_tie_break_value="10000001",
        classification="annual_report_full",
        identity_hash="b" * 64,
    )
    index = await updates.append_change(
        "local_owner", change_key=f"test:poll-index:{poll_id}", kind="source_page",
        listing_id=listing.id, context="company_research",
        payload={
            "record_type": "poll_index_v1", "poll_id": str(poll_id),
            "authorization_id": str(grant.authorization_id),
            "authorization_revision": grant.revision,
            "authorization_hash": grant.authorization_hash,
            "contract_hash": grant.contract_hash,
            "source_scope_hash": source_scope,
            "attempt_scope_hash": attempt_scope,
            "protocol_profile": "fixture", "protocol_status": "fixture_validated",
            "listing_ref": listing_ref.model_dump(mode="json"),
            "window_start": grant.history_start.isoformat(),
            "window_end": grant.history_end.isoformat(),
            "page_num": 1, "has_more": True, "previous_index_ref": None,
            "ordered_document_keys": [{
                "native_announcement_id": document.native_announcement_id,
                "identity_hash": document.identity_hash,
            }],
            "ordered_document_keys_hash": canonical_hash([{
                "native_announcement_id": document.native_announcement_id,
                "identity_hash": document.identity_hash,
            }]),
            "response_hash": hashlib.sha256(query_body).hexdigest(),
            "response_bytes": len(query_body), "http_attempt_refs": query_refs,
        },
        acquired_at=now, available_from=now, committed_at=now,
    )
    db_session.info["allow_disclosure_fixture_proofs"] = True

    async def add_index(doc: CNInfoDiscoveredDocument, page_num: int, previous) -> Any:
        keys = [{"native_announcement_id": doc.native_announcement_id,
                 "identity_hash": doc.identity_hash}]
        return await updates.append_change(
            "local_owner", change_key=f"test:poll-index:{poll_id}:{page_num}",
            kind="source_page", listing_id=listing.id, context="company_research",
            payload={
                "record_type": "poll_index_v1", "poll_id": str(poll_id),
                "authorization_id": str(grant.authorization_id),
                "authorization_revision": grant.revision,
                "authorization_hash": grant.authorization_hash,
                "contract_hash": grant.contract_hash,
                "source_scope_hash": source_scope, "attempt_scope_hash": attempt_scope,
                "protocol_profile": "fixture", "protocol_status": "fixture_validated",
                "listing_ref": listing_ref.model_dump(mode="json"),
                "window_start": grant.history_start.isoformat(),
                "window_end": grant.history_end.isoformat(),
                "page_num": page_num, "has_more": page_num < 3,
                "previous_index_ref": previous,
                "ordered_document_keys": keys,
                "ordered_document_keys_hash": canonical_hash(keys),
                "response_hash": hashlib.sha256(query_body).hexdigest(),
                "response_bytes": len(query_body), "http_attempt_refs": query_refs,
            },
            acquired_at=now, available_from=now, committed_at=now,
        )

    async def perform_attempt(
        attempt_no: int, current_document: CNInfoDiscoveredDocument, current_index,
        current_body: bytes | None = None,
        *, redirect: bool = False, tamper_response_ref: bool = False,
    ) -> dict[str, Any]:
        # Query hop was settled at ``now``; leave the required five-second
        # source cooldown between every hop and the next attempt.
        attempt_now = now + timedelta(seconds=12 * attempt_no)
        attempt_id = uuid4()
        body_bytes = current_body or _pdf()
        fetch_refs: list[dict[str, Any]] = []
        fetch_response_refs: list[dict[str, Any]] = []
        final_sent = None
        final_settlement = None
        final_url = current_document.locator
        redirect_url = "https://static.cninfo.com.cn/finalpage/2026-10-16/10000001-redirected.PDF"
        for hop_ordinal in range(2 if redirect else 1):
            hop_url = current_document.locator if hop_ordinal == 0 else redirect_url
            is_redirect = redirect and hop_ordinal == 0
            response_bytes = b"controlled redirect body" if is_redirect else body_bytes
            status = 307 if is_redirect else 200
            response_hash = hashlib.sha256(response_bytes).hexdigest()
            identity = DisclosureAttemptIdentity(
                poll_id=str(poll_id), logical_request_key="fetch:10000001",
                attempt_id=attempt_id, attempt_ordinal=attempt_no, hop_ordinal=hop_ordinal,
                hop_id=f"fetch:{attempt_id}:{hop_ordinal}", request_kind="fetch",
                request_hash=canonical_hash({"doc": current_document.identity_hash}),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                contract_hash=grant.contract_hash, scope_hash=attempt_scope,
                listing_ref=listing_ref, window_start=grant.history_start,
                window_end=grant.history_end, poll_http_cap=10,
                poll_query_http_cap=2, poll_fetch_cap=8,
                poll_response_bytes_cap=6 * 1024 * 1024,
                url=hop_url, response_bytes_limit=1024 * 1024,
            )
            reserved = await reserve_http_hop(
                db_session, identity=identity, response_byte_limit=identity.response_bytes_limit,
                lease=lease, base_settings=settings, now=attempt_now + timedelta(seconds=6 * hop_ordinal),
            )
            sent = await mark_http_hop_sent(
                db_session, permit=reserved, lease=lease, base_settings=settings,
                now=attempt_now + timedelta(seconds=6 * hop_ordinal),
            )
            settlement = await settle_http_hop_result(
                db_session, permit=sent, lease=lease, response_bytes=len(response_bytes),
                result_code=f"http_{status}", response_hash=response_hash,
                storage_bytes=len(response_bytes), now=attempt_now + timedelta(seconds=6 * hop_ordinal),
            )
            for phase in ("reserved", "sent", "result"):
                receipt = await updates.get_change_by_key(
                    "local_owner", f"p4:attempt:{attempt_id}:{hop_ordinal}:{phase}",
                )
                assert receipt is not None
                fetch_refs.append({
                    "sequence": receipt.sequence, "id": str(receipt.id),
                    "payload_hash": receipt.payload_hash,
                })
            result_receipt = await updates.get_change_by_key(
                "local_owner", f"p4:attempt:{attempt_id}:{hop_ordinal}:result",
            )
            assert result_receipt is not None
            if await db_session.get(DisclosureBlob, response_hash) is None:
                db_session.add(DisclosureBlob(content_hash=response_hash, body=response_bytes))
                await db_session.flush()
            response_receipt = await updates.append_change(
                "local_owner",
                change_key=f"p4:disclosure-http-response:{attempt_id}:{hop_ordinal}",
                kind="source_attempt",
                payload={
                    "record_type": "poll_http_response_v1", "poll_id": str(poll_id),
                    "attempt_id": str(attempt_id), "hop_ordinal": hop_ordinal,
                    "request_kind": "fetch", "logical_request_key": identity.logical_request_key,
                    "status": status, "response_url": hop_url,
                    "content_type": "application/pdf" if not is_redirect else "text/plain",
                    "location": redirect_url if is_redirect else None,
                    "raw_blob_hash": response_hash,
                    "response_hash": response_hash,
                    "response_bytes": len(response_bytes),
                    "settlement_ref": {"sequence": result_receipt.sequence,
                        "id": str(result_receipt.id), "payload_hash": result_receipt.payload_hash},
                },
            )
            fetch_response_refs.append({
                "sequence": response_receipt.sequence, "id": str(response_receipt.id),
                "payload_hash": response_receipt.payload_hash,
            })
            final_url = hop_url
            final_sent = sent
            final_settlement = settlement
        assert final_sent is not None and final_settlement is not None
        if tamper_response_ref:
            fetch_response_refs[0] = {**fetch_response_refs[0], "payload_hash": "f" * 64}
        return await ingest_disclosure_original(
            db_session, job=job, permit=final_sent, settlement=final_settlement,
                http_attempt_refs=fetch_refs,
                http_response_refs=fetch_response_refs,
                index_ref={"sequence": current_index.sequence, "id": str(current_index.id),
                           "payload_hash": current_index.payload_hash},
                document=current_document, body=body_bytes, response_url=final_url,
            now=attempt_now, settings=settings, poll_id=poll_id,
            protocol_profile="fixture", protocol_status="fixture_validated",
        )

    first = await perform_attempt(1, document, index)
    assert first["event_created"] is True
    events_before = await db_session.scalar(select(func.count()).select_from(ResearchEventRevision))
    generation_before = await db_session.scalar(select(SupplementalRegistryHead.events_generation))
    assert events_before == 1 and generation_before == 1
    source_state = await updates.get_source(
        "local_owner", "cninfo_szse_v1", f"listing:{listing.id}:company_research",
    )
    assert source_state is not None and source_state.payload["semantic_generation"] == 1
    saved_event = (await db_session.execute(select(ResearchEventRevision))).scalar_one()
    assert saved_event.payload["verification"] == "candidate"
    assert saved_event.payload["policy_stage"] == "unknown"
    assert saved_event.payload["event_type"] == "company_disclosure"
    assert saved_event.payload["source_refs"] == [
        first["original_ref"], first["evidence_ref"],
    ]
    immutable_first_payload = dict(saved_event.payload)

    # A title-only source metadata correction appends an immutable candidate
    # revision. Returning from B to the old A semantics is quarantined.
    document_url_changed = document.model_copy(update={
        "locator": "https://static.cninfo.com.cn/finalpage/2026-10-16/10000002.PDF",
        "identity_hash": "e" * 64,
    })
    index_url_changed = await add_index(document_url_changed, 2, {
        "sequence": index.sequence, "id": str(index.id), "payload_hash": index.payload_hash,
    })
    second = await perform_attempt(2, document_url_changed, index_url_changed, redirect=True)
    assert second["event_created"] is False
    assert second["event_ref"] == first["event_ref"]
    assert second["content_hash"] == first["content_hash"]
    source_state = await updates.get_source(
        "local_owner", "cninfo_szse_v1", f"listing:{listing.id}:company_research",
    )
    assert source_state is not None and source_state.payload["semantic_generation"] == 1
    document_b = document_url_changed.model_copy(update={
        "title": "年度报告全文更正（测试夹具）", "identity_hash": "d" * 64,
    })
    index_b = await add_index(document_b, 3, {
        "sequence": index_url_changed.sequence, "id": str(index_url_changed.id),
        "payload_hash": index_url_changed.payload_hash,
    })
    body_b = _pdf("B")
    third = await perform_attempt(3, document_b, index_b, body_b)
    assert third["event_created"] is True
    assert third["content_hash"] != first["content_hash"]
    source_state = await updates.get_source(
        "local_owner", "cninfo_szse_v1", f"listing:{listing.id}:company_research",
    )
    assert source_state is not None and source_state.payload["semantic_generation"] == 2
    document_a_again = document
    index_a_again = await add_index(document_a_again, 4, {
        "sequence": index_b.sequence, "id": str(index_b.id), "payload_hash": index_b.payload_hash,
    })
    from investment_agent.investor.foundations import InvestorError
    with pytest.raises(InvestorError, match="回现"):
        await perform_attempt(4, document_a_again, index_a_again)
    assert await db_session.scalar(select(func.count()).select_from(DisclosureBlob)) == 3
    assert await db_session.scalar(select(func.count()).select_from(DisclosureItem)) == 1
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 2
    assert await db_session.scalar(select(func.count()).select_from(SavedEvidence)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ResearchEventRevision)) == 2
    assert await db_session.scalar(select(SupplementalRegistryHead.events_generation)) == 2
    events = (await db_session.scalars(select(ResearchEventRevision).order_by(
        ResearchEventRevision.revision
    ))).all()
    assert [row.revision for row in events] == [1, 2]
    assert [row.payload["title"] for row in events] == [document.title, document_b.title]
    assert events[0].payload == immutable_first_payload
    assert events[1].payload["source_refs"] != events[0].payload["source_refs"]
    original_b = await db_session.get(DisclosureOriginal, third["original_ref"]["id"])
    assert original_b is not None
    assert original_b.payload["prior_original_refs"] == [first["original_ref"]]
    leaf_count = await db_session.scalar(select(func.count()).select_from(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.kind == "source_attempt",
        SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_document_v1",
    ))
    assert leaf_count == 3

    # A late failure at the final proof leaf rolls back all candidate writes
    # when the caller uses its documented transaction boundary.
    document_new = document.model_copy(update={
        "native_announcement_id": "10000002",
        "locator": "https://static.cninfo.com.cn/finalpage/2026-10-16/10000003.PDF",
        "identity_hash": "f" * 64,
    })
    index_new = await add_index(document_new, 5, {
        "sequence": index_a_again.sequence, "id": str(index_a_again.id),
        "payload_hash": index_a_again.payload_hash,
    })
    with pytest.raises(InvestorError, match="摘要不匹配"):
        async with db_session.begin_nested():
            await perform_attempt(5, document_new, index_new, redirect=True, tamper_response_ref=True)
    original_append = SupplementalUpdatesRepository.append_change

    async def fail_on_document_leaf(self, owner_id, **kwargs):
        if str(kwargs.get("change_key", "")).startswith("p4:cninfo:document:"):
            raise RuntimeError("injected final-leaf failure")
        return await original_append(self, owner_id, **kwargs)

    monkeypatch.setattr(SupplementalUpdatesRepository, "append_change", fail_on_document_leaf)
    with pytest.raises(RuntimeError, match="final-leaf"):
        async with db_session.begin_nested():
            await perform_attempt(6, document_new, index_new)
    monkeypatch.setattr(SupplementalUpdatesRepository, "append_change", original_append)
    assert await db_session.scalar(select(func.count()).select_from(DisclosureItem)) == 1
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 2
    assert await db_session.scalar(select(func.count()).select_from(SavedEvidence)) == 2
    assert await db_session.scalar(select(func.count()).select_from(ResearchEventRevision)) == 2
    assert await db_session.scalar(select(SupplementalRegistryHead.events_generation)) == 2
    source_state = await updates.get_source(
        "local_owner", "cninfo_szse_v1", f"listing:{listing.id}:company_research",
    )
    assert source_state is not None and source_state.payload["semantic_generation"] == 2
    leaf_count_after_rollback = await db_session.scalar(select(func.count()).select_from(
        SupplementalChangeReceiptRow
    ).where(
        SupplementalChangeReceiptRow.kind == "source_attempt",
        SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_document_v1",
    ))
    assert leaf_count_after_rollback == 3
