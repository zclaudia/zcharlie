"""Stage steps for one disclosure poll.

The handler keeps the lease, authorization, and HTTP retry loop. These
functions keep the checkpoint fields and stop reasons unchanged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from investment_agent.evidence.disclosure_http import DisclosureError, validate_url
from investment_agent.evidence.disclosure_pages import (
    DisclosureBackgroundFetchV1,
    page_url_body,
    parse_page_response,
    validate_pdf,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import JobOutcome
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.supplemental_rules.canonical import canonical_hash


async def stage_request_target(
    handler: Any,
    session: Any,
    job: Any,
    state: Any,
    stage: Any,
    checkpoint: Any,
    grant: Any,
    root: Any,
    listing: Any,
    listing_ref: Any,
    protocol: Any,
) -> (
    Literal["partial"]
    | tuple[
        str,
        Literal["directory", "query", "original"],
        bytes | None,
        Literal["directory", "query", "fetch"],
        int,
        str,
    ]
):
    from investment_agent.supplemental_rules.disclosure_updates import (
        MAX_QUERY_PAGES,
        _make_page_request,
    )

    url: str
    purpose: Literal["directory", "query", "original"]
    body: bytes | None
    request_kind: Literal["directory", "query", "fetch"]
    response_limit: int
    logical_key: str
    if stage == "directory":
        url, purpose, body, request_kind, response_limit = (
            "https://www.cninfo.com.cn/new/data/szse_stock.json",
            "directory",
            None,
            "directory",
            min(8 * 1024 * 1024, grant.per_poll_response_bytes),
        )
        logical_key = "directory"
    elif stage in {"query", "verify_query"}:
        if int(checkpoint["page_num"]) > MAX_QUERY_PAGES:
            await handler._stop_poll(
                session,
                job,
                state,
                "window_split_required",
                "query_page_cap_reached",
            )
            return "partial"
        page_request = _make_page_request(
            root.payload, listing, protocol, int(checkpoint["page_num"])
        )
        url, purpose, body = page_url_body(
            page_request,
            issuer_id=str(checkpoint.get("issuer_id", "")),
            ticker=listing.ticker,
            protocol=protocol,
        )
        request_kind, response_limit = (
            "query",
            min(page_request.max_bytes, grant.per_poll_response_bytes),
        )
        logical_key = (
            f"page:{checkpoint['page_num']}"
            if stage == "query"
            else f"verify-page:{checkpoint['page_num']}"
        )
    else:
        fetches_done = int(checkpoint.get("eligible_count", 0))
        if fetches_done >= grant.per_poll_fetch:
            await handler._stop_poll(
                session,
                job,
                state,
                "window_split_required",
                "fetch_budget_exhausted",
            )
            return "partial"
        pending = checkpoint.get("pending_documents")
        if not isinstance(pending, list) or not pending:
            await handler._stop_poll(
                session, job, state, "empty_fetch_checkpoint", "invalid_reference"
            )
            return "partial"
        from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument

        document = CNInfoDiscoveredDocument.model_validate(pending[0])
        url = document.locator
        DisclosureBackgroundFetchV1(
            listing_id=listing.id,
            authorization_id=grant.authorization_id,
            authorization_revision=grant.revision,
            authorization_hash=grant.authorization_hash,
            listing_ref=listing_ref,
            window_start=datetime.fromisoformat(root.payload["window_start"]),
            window_end=datetime.fromisoformat(root.payload["window_end"]),
            contract_hash=grant.contract_hash,
            source_scope_hash=root.payload["source_scope_hash"],
            attempt_scope_hash=root.payload["attempt_scope_hash"],
            native_announcement_id=document.native_announcement_id,
            locator=document.locator,
            protocol_hash=protocol.protocol_hash,
        )
        validate_url(url, "original")
        purpose, body, request_kind = "original", None, "fetch"
        response_limit = min(16 * 1024 * 1024, grant.per_poll_response_bytes)
        logical_key = f"document:{document.native_announcement_id}"
    return url, purpose, body, request_kind, response_limit, logical_key

async def apply_stage_response(
    handler: Any,
    session: Any,
    job: Any,
    state: Any,
    stage: Any,
    checkpoint: Any,
    grant: Any,
    root: Any,
    listing: Any,
    listing_id: Any,
    listing_ref: Any,
    protocol: Any,
    protocol_status: Any,
    profile: Any,
    repo: Any,
    response: Any,
    now: Any,
    attempt_ordinal: Any,
    http_refs: Any,
    http_response_refs: Any,
    sent_permit: Any,
    settlement: Any,
) -> JobOutcome:
    from investment_agent.supplemental_rules.disclosure_updates import (
        DISCLOSURE_FEED,
        LOCAL_OWNER,
        _append_filtered_leaf,
        _handoff_poll_step,
        _make_page_request,
        _poll_common_payload,
        _receipt_ref,
        _store_blob,
        source_scope_key,
    )

    if stage == "directory":
        try:
            issuer = handler._test_service.issuer_from_directory(response, listing.ticker)
        except DisclosureError as exc:
            await handler._stop_poll(session, job, state, str(exc), str(exc))
            return "partial"
        directory_hash = await _store_blob(session, response.body)
        directory_receipt = await repo.append_change(
            LOCAL_OWNER,
            change_key=f"p4:disclosure-directory:{root.id}",
            kind="source_attempt",
            parent_sequence=root.sequence,
            listing_id=listing_id,
            context="company_research",
            payload={
                "record_type": "poll_directory_v1",
                "poll_id": str(root.id),
                "authorization_id": str(grant.authorization_id),
                "authorization_revision": grant.revision,
                "authorization_hash": grant.authorization_hash,
                "contract_hash": grant.contract_hash,
                "source_scope_hash": root.payload["source_scope_hash"],
                "attempt_scope_hash": root.payload["attempt_scope_hash"],
                "listing_ref": listing_ref.model_dump(mode="json"),
                "context": "company_research",
                "window_start": root.payload["window_start"],
                "window_end": root.payload["window_end"],
                "issuer_id": issuer,
                "directory_hash": directory_hash,
                "raw_blob_hash": directory_hash,
                "response_bytes": len(response.body),
                "response_hash": hashlib.sha256(response.body).hexdigest(),
                "http_attempt_refs": http_refs,
                "http_response_refs": http_response_refs,
            },
            acquired_at=now,
            available_from=now,
            committed_at=now,
        )
        checkpoint.update(
            {
                "stage": "query",
                "issuer_id": issuer,
                "directory_hash": directory_hash,
                "directory_ref": _receipt_ref(directory_receipt),
                "acquired_at": now.isoformat(),
                "redirect_count": 0,
                "attempt_id": None,
                "current_url": None,
                "attempt_ordinal": attempt_ordinal,
            }
        )
    elif stage in {"query", "verify_query"}:
        page_request = _make_page_request(
            root.payload,
            listing,
            protocol,
            int(checkpoint["page_num"]),
        )
        try:
            parsed_page = parse_page_response(
                page_request,
                response,
                issuer_id=str(checkpoint["issuer_id"]),
                ticker=listing.ticker,
                protocol=protocol,
                now=now,
            )
        except DisclosureError as exc:
            await handler._stop_poll(session, job, state, str(exc), str(exc))
            return "partial"
        raw_hash = await _store_blob(session, response.body)
        ordered_keys = [
            {
                "native_announcement_id": doc.native_announcement_id,
                "identity_hash": doc.identity_hash,
            }
            for doc in parsed_page.documents
        ]
        page_payload = _poll_common_payload(
            root.payload,
            profile=profile,
            protocol_status=protocol_status,
        )
        page_payload.update(
            {
                "record_type": "poll_index_v1",
                "query_pass": "primary" if stage == "query" else "verification",
                "page_num": parsed_page.page_num,
                "has_more": parsed_page.has_more,
                "total": parsed_page.total,
                "raw_blob_hash": raw_hash,
                "response_hash": parsed_page.response_hash,
                "response_bytes": parsed_page.response_bytes,
                "http_attempt_refs": http_refs,
                "http_response_refs": http_response_refs,
                "previous_index_ref": (
                    checkpoint.get("previous_index_ref")
                    if stage == "query"
                    else checkpoint.get("previous_verify_index_ref")
                ),
                "ordered_document_keys": ordered_keys,
                "ordered_document_keys_hash": canonical_hash(ordered_keys),
                "identity_directory_hash": checkpoint.get("directory_hash"),
                "directory_ref": checkpoint.get("directory_ref"),
                "issuer_id": checkpoint["issuer_id"],
            }
        )
        index = await repo.append_change(
            LOCAL_OWNER,
            change_key=(
                f"p4:disclosure-index:{root.id}:{parsed_page.page_num}"
                if stage == "query"
                else f"p4:disclosure-index:{root.id}:{parsed_page.page_num}:verify"
            ),
            kind="source_page",
            parent_sequence=root.sequence,
            listing_id=listing_id,
            context="company_research",
            payload=page_payload,
            acquired_at=parsed_page.acquired_at,
            available_from=parsed_page.acquired_at,
            committed_at=now,
        )
        index_ref = _receipt_ref(index)
        unknown = [doc for doc in parsed_page.documents if doc.classification == "unknown"]
        if unknown:
            await handler._stop_poll(
                session,
                job,
                state,
                "unknown_source_document_type",
                "unknown_source_document_type",
            )
            return "partial"
        observed_rows = (
            sum(
                int(page.get("returned_count", 0))
                for page in checkpoint.get("primary_pages", [])
                if isinstance(page, dict)
            )
            + parsed_page.returned_count
        )
        if observed_rows > 200:
            await handler._stop_poll(
                session,
                job,
                state,
                "window_split_required",
                "poll_document_manifest_limit",
            )
            return "partial"
        if stage == "verify_query":
            primary_pages = checkpoint.get("primary_pages")
            page_num = int(checkpoint["page_num"])
            if not isinstance(primary_pages, list) or not 1 <= page_num <= len(
                primary_pages
            ):
                await handler._stop_poll(
                    session,
                    job,
                    state,
                    "primary_page_checkpoint_missing",
                    "invalid_reference",
                )
                return "partial"
            primary_page = primary_pages[page_num - 1]
            if (
                ordered_keys != primary_page.get("ordered_document_keys")
                or parsed_page.has_more != primary_page.get("has_more")
                or parsed_page.total != primary_page.get("total")
                or parsed_page.returned_count != primary_page.get("returned_count")
            ):
                await handler._stop_poll(
                    session,
                    job,
                    state,
                    "source_page_changed_between_passes",
                    "source_page_changed_between_passes",
                )
                return "partial"
            checkpoint.update(
                {
                    "stage": "seal_page",
                    "verification_index_ref": index_ref,
                    "verification_http_attempt_refs": http_refs,
                    "verification_http_response_refs": http_response_refs,
                    "verification_response_hash": parsed_page.response_hash,
                    "verification_ordered_document_keys_hash": canonical_hash(ordered_keys),
                    "verification_raw_blob_hash": raw_hash,
                    "redirect_count": 0,
                    "attempt_id": None,
                    "current_url": None,
                    "attempt_ordinal": attempt_ordinal,
                }
            )
            await _handoff_poll_step(
                session,
                job=job,
                root=root,
                source_state=state,
                checkpoint=checkpoint,
            )
            return "completed"
        filtered_refs = []
        pending_docs = []
        for doc in parsed_page.documents:
            if (
                doc.classification == "excluded"
                or doc.classification not in grant.doc_type_allowlist
            ):
                leaf = await _append_filtered_leaf(
                    repo,
                    root.payload,
                    doc,
                    index_ref=index_ref,
                    profile=profile,
                    protocol_status=protocol_status,
                    parent_sequence=index.sequence,
                    listing_id=listing_id,
                    now=now,
                )
                filtered_refs.append(leaf)
            else:
                pending_docs.append(doc.model_dump(mode="json"))
        order_keys = [
            [doc.published_raw_millis, doc.stable_tie_break_value]
            for doc in parsed_page.documents
        ]
        primary_pages = checkpoint.get("primary_pages", [])
        seen_ids = {
            key.get("native_announcement_id")
            for page in primary_pages
            for key in page.get("ordered_document_keys", [])
            if isinstance(page, dict) and isinstance(key, dict)
        }
        page_ids = {key["native_announcement_id"] for key in ordered_keys}
        if seen_ids.intersection(page_ids):
            await handler._stop_poll(
                session,
                job,
                state,
                "duplicate_native_identity_across_pages",
                "invalid_reference",
            )
            return "partial"
        prior_order = next(
            (
                page.get("order_keys", [])[-1]
                for page in reversed(primary_pages)
                if isinstance(page, dict) and page.get("order_keys")
            ),
            None,
        )
        if prior_order is not None and order_keys:
            if protocol.query_sort_type == "desc":
                correctly_ordered = prior_order > order_keys[0]
            else:
                correctly_ordered = prior_order < order_keys[0]
            if not correctly_ordered:
                await handler._stop_poll(
                    session,
                    job,
                    state,
                    "source_order_changed_across_pages",
                    "invalid_reference",
                )
                return "partial"
        primary_page = {
            "page_num": parsed_page.page_num,
            "index_ref": index_ref,
            "response_hash": parsed_page.response_hash,
            "raw_blob_hash": raw_hash,
            "ordered_document_keys": ordered_keys,
            "order_keys": order_keys,
            "has_more": parsed_page.has_more,
            "total": parsed_page.total,
            "returned_count": parsed_page.returned_count,
            "document_refs": filtered_refs,
            "eligible_count": 0,
            "filtered_count": len(filtered_refs),
            "acquired_at": parsed_page.acquired_at.isoformat(),
        }
        totals = {page.get("total") for page in primary_pages if isinstance(page, dict)}
        if parsed_page.total is not None and totals and totals != {parsed_page.total}:
            await handler._stop_poll(
                session,
                job,
                state,
                "source_total_changed_between_pages",
                "invalid_reference",
            )
            return "partial"
        checkpoint.update(
            {
                "stage": "fetch"
                if pending_docs
                else ("query" if parsed_page.has_more else "verify_query"),
                "index_ref": index_ref,
                "response_hash": parsed_page.response_hash,
                "raw_blob_hash": raw_hash,
                "ordered_document_keys": ordered_keys,
                "pending_documents": pending_docs,
                "page_document_refs": filtered_refs,
                "page_eligible_count": 0,
                "page_filtered_count": len(filtered_refs),
                "page_has_more": parsed_page.has_more,
                "page_total": parsed_page.total,
                "page_acquired_at": parsed_page.acquired_at.isoformat(),
                "current_primary_page": primary_page if pending_docs else None,
                "redirect_count": 0,
                "attempt_id": None,
                "current_url": None,
                "attempt_ordinal": attempt_ordinal,
            }
        )
        if not pending_docs:
            primary_pages.append(primary_page)
            checkpoint["primary_pages"] = primary_pages
            checkpoint["previous_index_ref"] = index_ref
            if parsed_page.has_more:
                checkpoint["page_num"] = parsed_page.page_num + 1
            else:
                if (
                    parsed_page.total is not None
                    and sum(int(page.get("returned_count", 0)) for page in primary_pages)
                    != parsed_page.total
                ):
                    await handler._stop_poll(
                        session, job, state, "source_total_incomplete", "invalid_reference"
                    )
                    return "partial"
                checkpoint["page_num"] = 1
                first_page = primary_pages[0]
                checkpoint.update(
                    {
                        "index_ref": first_page["index_ref"],
                        "response_hash": first_page["response_hash"],
                        "raw_blob_hash": first_page["raw_blob_hash"],
                        "ordered_document_keys": first_page["ordered_document_keys"],
                        "page_document_refs": first_page["document_refs"],
                        "page_eligible_count": first_page["eligible_count"],
                        "page_filtered_count": first_page["filtered_count"],
                        "page_has_more": first_page["has_more"],
                        "page_total": first_page["total"],
                        "page_acquired_at": first_page["acquired_at"],
                    }
                )
    else:
        from investment_agent.evidence.disclosure_pages import CNInfoDiscoveredDocument
        from investment_agent.supplemental_rules.disclosure_candidate_ingest import (
            ingest_disclosure_original,
        )

        pending = checkpoint.get("pending_documents")
        if not isinstance(pending, list) or not pending:
            await handler._stop_poll(
                session, job, state, "empty_fetch_checkpoint", "invalid_reference"
            )
            return "partial"
        document = CNInfoDiscoveredDocument.model_validate(pending[0])
        try:
            async with session.begin_nested():
                validate_pdf(response.body, response.headers.get("content-type", ""))
                result = await ingest_disclosure_original(
                    session,
                    job=job,
                    permit=sent_permit,
                    settlement=settlement,
                    index_ref=checkpoint["index_ref"],
                    http_attempt_refs=http_refs,
                    http_response_refs=http_response_refs,
                    document=document,
                    body=response.body,
                    response_url=response.url,
                    now=now,
                    settings=handler.settings,
                    poll_id=UUID(str(root.id)),
                    protocol_profile=profile,
                    protocol_status=protocol_status,
                )
        except (DisclosureError, InvestorError) as exc:
            await handler._stop_poll(session, job, state, str(exc), str(exc))
            return "partial"
        state = await repo.get_source(
            LOCAL_OWNER,
            DISCLOSURE_FEED,
            source_scope_key(listing_ref),
            for_update=True,
        )
        if state is None:
            await PipelineRunRepository(session).mark_partial(
                job.run_id,
                "disclosure_source_state_missing_after_ingest",
                lease=job.lease,
            )
            return "partial"
        pending.pop(0)
        doc_ref = result["leaf_ref"]
        checkpoint["page_document_refs"] = [
            *checkpoint.get("page_document_refs", []),
            doc_ref,
        ]
        checkpoint["page_eligible_count"] = (
            int(checkpoint.get("page_eligible_count", 0)) + 1
        )
        checkpoint["stage"] = (
            "fetch"
            if pending
            else ("query" if checkpoint.get("page_has_more") else "verify_query")
        )
        checkpoint["pending_documents"] = pending
        current_primary = checkpoint.get("current_primary_page")
        if not pending:
            if not isinstance(current_primary, dict):
                await handler._stop_poll(
                    session,
                    job,
                    state,
                    "primary_page_checkpoint_missing",
                    "invalid_reference",
                )
                return "partial"
            current_primary["document_refs"] = list(
                checkpoint.get("page_document_refs", [])
            )
            current_primary["eligible_count"] = int(
                checkpoint.get("page_eligible_count", 0)
            )
            current_primary["filtered_count"] = int(
                checkpoint.get("page_filtered_count", 0)
            )
            primary_pages = list(checkpoint.get("primary_pages", []))
            primary_pages.append(current_primary)
            checkpoint["primary_pages"] = primary_pages
            checkpoint["previous_index_ref"] = checkpoint.get("index_ref")
            if checkpoint.get("page_has_more"):
                checkpoint["page_num"] = int(checkpoint["page_num"]) + 1
            else:
                page_total = checkpoint.get("page_total")
                if (
                    page_total is not None
                    and sum(int(page.get("returned_count", 0)) for page in primary_pages)
                    != page_total
                ):
                    await handler._stop_poll(
                        session, job, state, "source_total_incomplete", "invalid_reference"
                    )
                    return "partial"
                checkpoint["page_num"] = 1
                first_page = primary_pages[0]
                checkpoint.update(
                    {
                        "index_ref": first_page["index_ref"],
                        "response_hash": first_page["response_hash"],
                        "raw_blob_hash": first_page["raw_blob_hash"],
                        "ordered_document_keys": first_page["ordered_document_keys"],
                        "page_document_refs": first_page["document_refs"],
                        "page_eligible_count": first_page["eligible_count"],
                        "page_filtered_count": first_page["filtered_count"],
                        "page_has_more": first_page["has_more"],
                        "page_total": first_page["total"],
                        "page_acquired_at": first_page["acquired_at"],
                    }
                )
            checkpoint["current_primary_page"] = None
        checkpoint["redirect_count"] = 0
        checkpoint["attempt_id"] = None
        checkpoint["current_url"] = None
        checkpoint["attempt_ordinal"] = attempt_ordinal
    await _handoff_poll_step(
        session,
        job=job,
        root=root,
        source_state=state,
        checkpoint=checkpoint,
    )
    return "completed"
