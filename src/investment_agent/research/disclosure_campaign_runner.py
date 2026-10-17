"""Exact-run entrypoint for approved manual disclosure campaigns.

This deliberately has no worker loop, global recovery, or maintenance pass. A
caller supplies the exact successor PipelineRun id returned by admission or
manual recovery; all persisted bindings are checked again immediately before
claiming that one run with the native worker lease and heartbeat.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.engine import make_url

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import get_current_authorization
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationV2,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_FEED,
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    _campaign_dispatch_capability,
    _campaign_resume_capability,
    resume_disclosure_poll,
    source_scope_key,
)


def _assert_database_shape(session: Any) -> None:
    url = make_url(str(session.get_bind().url))
    database = url.database or ""
    if (
        url.get_backend_name() != "sqlite"
        or not database
        or database == ":memory:"
        or database.startswith("file:")
        or url.query.get("mode") == "memory"
        or url.query.get("uri") in {True, "true", "1"}
    ):
        raise InvestorError(
            "disclosure_campaign_environment_mismatch",
            "manual campaign runner requires a file-backed SQLite database",
        )
    return None


def _same_instant(left: object, right: object) -> bool:
    try:
        a = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(right).replace("Z", "+00:00"))
    except ValueError:
        return False
    return a.tzinfo is not None and b.tzinfo is not None and a.astimezone(UTC) == b.astimezone(UTC)


async def _assert_exact_successor(
    run_id: int,
    *,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    now: datetime,
) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_campaign import assert_campaign_action

    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        request = dict(run.request_json or {}) if run is not None else {}
        if (
            run is None
            or run.kind != DISCLOSURE_POLL_KIND
            or request.get("record_type") != "poll_step_v1"
            or str(request.get("poll_id")) != str(poll_id)
            or request.get("campaign_id") != str(campaign_id)
            or request.get("campaign_action_id") != action_id
            or request.get("sample_env_id") != sample_env_id
        ):
            raise InvestorError(
                "disclosure_campaign_run_mismatch",
                "runner requires the exact campaign poll successor run",
            )
        _assert_database_shape(session)
        action = await assert_campaign_action(
            session,
            campaign_id=campaign_id,
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            poll_id=str(poll_id),
            sample_env_id=sample_env_id,
            now=now,
        )
        repo = SupplementalUpdatesRepository(session)
        sequence = request.get("poll_sequence")
        if type(sequence) is not int or sequence < 1:
            raise InvestorError("disclosure_campaign_run_mismatch", "poll root sequence is invalid")
        root = await repo.get_change("local_owner", sequence)
        if (
            root is None
            or root.payload_hash != request.get("poll_hash")
            or root.payload.get("record_type") != "poll_root_v1"
            or str(root.id) != str(poll_id)
            or root.payload.get("campaign_binding", {}).get("action_id") != action_id
            or root.payload.get("campaign_binding", {}).get("campaign_id") != str(campaign_id)
            or root.payload.get("campaign_binding", {}).get("sample_env_id") != sample_env_id
            or root.payload.get("campaign_binding", {}).get("profile_hash") != action.get("profile_hash")
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "poll root does not match campaign action binding",
            )
        grant = await get_current_authorization(session, grant_id, now=now)
        if (
            not isinstance(grant, BackgroundDisclosureAuthorizationV2)
            or grant.revision != grant_revision
            or grant.campaign_id != campaign_id
            or grant.sample_env_id != sample_env_id
            or root.payload.get("authorization_hash") != grant.authorization_hash
            or root.payload.get("contract_hash") != grant.contract_hash
            or root.payload.get("listing_ref") != action.get("listing_ref")
            or not _same_instant(root.payload.get("window_start"), action.get("window_start"))
            or not _same_instant(root.payload.get("window_end"), action.get("window_end"))
            or root.payload.get("contract_hash") != action.get("contract_hash")
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "grant, root, and campaign action scope differ",
            )
        listing = ObjectRef.model_validate(root.payload.get("listing_ref"))
        state = await repo.get_source("local_owner", DISCLOSURE_FEED, source_scope_key(listing))
        if (
            state is None
            or state.payload.get("active_poll_id") != str(poll_id)
            or state.payload.get("active_step_run_id") != run_id
            or state.payload.get("poll_checkpoint", {}).get("campaign_binding")
            != root.payload.get("campaign_binding")
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "source checkpoint is not the exact campaign poll successor",
            )
        return action


async def run_disclosure_campaign_once(
    *,
    run_id: int,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    settings: Settings,
    worker_id: str = "manual-disclosure-campaign",
) -> int | None:
    """Run exactly one already queued campaign step through `run_once(run_id=...)`.

    The supplied IDs are selectors only. The sidecar, immutable poll root,
    current grant, current source checkpoint, environment and exact queued run
    are checked in the database before the worker claims anything.
    """
    if type(run_id) is not int or run_id < 1:
        raise InvestorError("invalid_reference", "exact run id must be a positive integer")
    now = datetime.now(UTC)
    await _assert_exact_successor(
        run_id,
        campaign_id=campaign_id,
        action_id=action_id,
        grant_id=grant_id,
        grant_revision=grant_revision,
        poll_id=poll_id,
        sample_env_id=sample_env_id,
        now=now,
    )
    handler = DisclosurePollingHandler(settings=settings)
    handler._install_campaign_dispatch_capability(_campaign_dispatch_capability(
        run_id=run_id,
        poll_id=poll_id,
        campaign_id=campaign_id,
        action_id=action_id,
        sample_env_id=sample_env_id,
        allow_network=True,
    ))
    worker = ResearchWorker(
        settings=settings,
        worker_id=worker_id,
        handlers=[handler],
        kinds=[DISCLOSURE_POLL_KIND],
    )
    return await worker.run_once(run_id=run_id)


async def resume_disclosure_campaign_poll(
    *,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    resume_approval_ref: str,
    operation_id: UUID,
    reason: str,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize one manual resume for the original poll and enqueue one step."""
    from investment_agent.supplemental_rules.disclosure_campaign import (
        authorize_campaign_resume,
        read_disclosure_campaign,
        resume_disclosure_campaign,
    )

    current = (now or datetime.now(UTC)).astimezone(UTC)
    async with session_scope() as session:
        _assert_database_shape(session)
        repo = SupplementalUpdatesRepository(session)
        root = await repo.get_change("local_owner", str(poll_id))
        if (
            root is None
            or root.payload.get("record_type") != "poll_root_v1"
            or str(root.id) != str(poll_id)
            or root.payload.get("authorization_id") != str(grant_id)
            or root.payload.get("authorization_revision") != grant_revision
            or root.payload.get("campaign_binding", {}).get("campaign_id") != str(campaign_id)
            or root.payload.get("campaign_binding", {}).get("action_id") != action_id
            or root.payload.get("campaign_binding", {}).get("sample_env_id") != sample_env_id
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "resume requires the exact original poll root",
            )
        resume_receipt = await repo.get_change_by_key(
            "local_owner", f"p4:disclosure-resume:{operation_id}",
        )
        if resume_receipt is not None:
            from investment_agent.supplemental_rules.canonical import canonical_hash

            expected_revision = resume_receipt.payload.get("expected_revision")
            campaign_resume = {
                "campaign_id": str(campaign_id),
                "action_id": action_id,
                "grant_id": str(grant_id),
                "grant_revision": grant_revision,
                "grant_hash": root.payload.get("authorization_hash"),
                "sample_env_id": sample_env_id,
                "operation_id": str(operation_id),
                "resume_approval_ref": resume_approval_ref,
            }
            expected_hash = canonical_hash({
                "method": "POST",
                "path": f"/supplemental-updates/polls/{poll_id}/resume",
                "body": {
                    "operation_id": str(operation_id),
                    "expected_revision": expected_revision,
                    "reason": reason,
                    "campaign_resume": campaign_resume,
                },
            })
            if (
                resume_receipt.payload.get("record_type") != "poll_resume_v1"
                or resume_receipt.payload.get("poll_id") != str(poll_id)
                or resume_receipt.payload.get("reason") != reason
                or resume_receipt.payload.get("campaign_resume") != campaign_resume
                or resume_receipt.request_hash != expected_hash
                or type(expected_revision) is not int
            ):
                raise InvestorError("idempotency_conflict", "resume operation reused with another scope")
            run = await PipelineRunRepository(session).by_idempotency_key(
                kind=DISCLOSURE_POLL_KIND,
                idempotency_key=f"p4:disclosure-resume:{operation_id}",
            )
            if run is None:
                raise InvestorError("idempotency_conflict", "resume receipt has no exact queued run")
            return {
                "poll_id": str(poll_id),
                "resume_sequence": resume_receipt.sequence,
                "run_id": run.id,
                "state": "queued",
                "replayed": True,
                "campaign_id": str(campaign_id),
                "action_id": action_id,
            }
        from investment_agent.supplemental_rules.disclosure_authorization import (
            read_authorization_snapshot,
        )

        grant = await read_authorization_snapshot(session, grant_id)
        if (
            not isinstance(grant, BackgroundDisclosureAuthorizationV2)
            or grant.revision != grant_revision
            or grant.campaign_id != campaign_id
            or grant.sample_env_id != sample_env_id
            or root.payload.get("authorization_hash") != grant.authorization_hash
            or root.payload.get("contract_hash") != grant.contract_hash
        ):
            raise InvestorError(
                "disclosure_campaign_binding_invalid",
                "resume requires the current original V2 grant",
            )
        listing = ObjectRef.model_validate(root.payload.get("listing_ref"))
        state = await repo.get_source(
            "local_owner", DISCLOSURE_FEED, source_scope_key(listing), for_update=True,
        )
        allowed_states = {
            "retry_wait", "partial", "failed", "paused",
            "manual_review_required", "manual_recovery_required",
        }
        if (
            state is None
            or state.payload.get("active_poll_id") != str(poll_id)
            or state.payload.get("poll_state") not in allowed_states
        ):
            raise InvestorError(
                "revision_conflict",
                "only the exact unfinished stopped poll may be resumed",
            )
        active_step_id = state.payload.get("active_step_run_id")
        if type(active_step_id) is int:
            active_step = await session.get(PipelineRun, active_step_id)
            if active_step is not None and active_step.status in {"queued", "running"}:
                raise InvestorError("source_poll_active", "the exact poll still has an active step")
        if state.retry_at is not None and state.retry_at.astimezone(UTC) > current:
            raise InvestorError("source_cooldown", "the original Retry-After has not expired")

        campaign = await read_disclosure_campaign(session, campaign_id)
        ticket = await authorize_campaign_resume(
            session,
            campaign_id=campaign_id,
            poll_id=str(poll_id),
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            grant_hash=grant.authorization_hash,
            sample_env_id=sample_env_id,
            resume_approval_ref=resume_approval_ref,
            operation_id=operation_id,
            now=current,
        )
        if ticket.get("allow_network_resume") is not True:
            raise InvestorError(
                "disclosure_campaign_resume_forbidden",
                "campaign did not authorize this exact resume",
            )
        await resume_disclosure_campaign(
            session,
            campaign_id=campaign_id,
            poll_id=str(poll_id),
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            grant_hash=grant.authorization_hash,
            sample_env_id=sample_env_id,
            operation_id=operation_id,
            expected_revision=campaign.revision,
            resume_approval_ref=resume_approval_ref,
            now=current,
        )
        capability = _campaign_resume_capability(
            poll_id=poll_id,
            campaign_id=campaign_id,
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            grant_hash=grant.authorization_hash,
            sample_env_id=sample_env_id,
            operation_id=operation_id,
            resume_approval_ref=resume_approval_ref,
        )
        resumed = await resume_disclosure_poll(
            session, poll_id, operation_id, state.revision, reason,
            settings=settings, now=current, _campaign_resume=capability,
        )
        return {**resumed, "campaign_id": str(campaign_id), "action_id": action_id}


async def admit_disclosure_campaign_recovery(
    *,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    recovery_approval_ref: str,
    operation_id: UUID,
    now: datetime | None = None,
) -> int:
    """Create one recovery-only run for the exact poll after human approval."""
    from investment_agent.supplemental_rules.disclosure_campaign import authorize_campaign_recovery

    current = (now or datetime.now(UTC)).astimezone(UTC)
    op_id = operation_id
    async with session_scope() as session:
        _assert_database_shape(session)
        decision = await authorize_campaign_recovery(
            session,
            campaign_id=campaign_id,
            poll_id=str(poll_id),
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            sample_env_id=sample_env_id,
            recovery_approval_ref=recovery_approval_ref,
            operation_id=op_id,
            now=current,
        )
        if decision.get("recovery_only") is not True or decision.get("allow_network") is not False:
            raise InvestorError(
                "disclosure_campaign_recovery_forbidden",
                "campaign has not authorized exact recovery-only",
            )
        repo = SupplementalUpdatesRepository(session)
        from investment_agent.supplemental_rules.disclosure_authorization import (
            read_authorization_snapshot,
        )

        grant = await read_authorization_snapshot(session, grant_id)
        if (
            not isinstance(grant, BackgroundDisclosureAuthorizationV2)
            or grant.revision != grant_revision
            or grant.campaign_id != campaign_id
            or grant.sample_env_id != sample_env_id
        ):
            raise InvestorError("disclosure_campaign_binding_invalid", "recovery grant/campaign binding differs")
        # Lookup by the exact immutable receipt id; no other scope is queried.
        root = await repo.get_change("local_owner", str(poll_id))
        if (
            root is None
            or root.payload.get("record_type") != "poll_root_v1"
            or str(root.id) != str(poll_id)
            or root.payload.get("campaign_binding", {}).get("action_id") != action_id
            or root.payload.get("campaign_binding", {}).get("campaign_id") != str(campaign_id)
            or root.payload.get("authorization_id") != str(grant_id)
            or root.payload.get("authorization_revision") != grant_revision
        ):
            raise InvestorError("disclosure_campaign_binding_invalid", "recovery root binding is invalid")
        listing_ref = ObjectRef.model_validate(root.payload.get("listing_ref"))
        state = await repo.get_source("local_owner", DISCLOSURE_FEED, source_scope_key(listing_ref), for_update=True)
        if state is None or state.payload.get("active_poll_id") != str(poll_id):
            raise InvestorError("revision_conflict", "recovery poll is not active for its exact source")
        active_step_id = state.payload.get("active_step_run_id")
        if type(active_step_id) is int:
            active_step = await session.get(PipelineRun, active_step_id)
            if active_step is not None and active_step.status in {"queued", "running"}:
                raise InvestorError("source_poll_active", "exact poll still has a queued or running step")
        request = {
            "record_type": "poll_recovery_v1",
            "schema_version": 1,
            "owner_id": "local_owner",
            "operation_id": str(op_id),
            "recovery_approval_ref": recovery_approval_ref,
            "poll_id": str(poll_id),
            "poll_sequence": root.sequence,
            "poll_hash": root.payload_hash,
            "campaign_id": str(campaign_id),
            "campaign_action_id": action_id,
            "grant_id": str(grant_id),
            "grant_revision": grant_revision,
            "sample_env_id": sample_env_id,
            "recovery_only": True,
            "allow_network": False,
        }
        run, _created = await PipelineRunRepository(session).enqueue(
            kind=DISCLOSURE_POLL_KIND,
            idempotency_key=f"p4:disclosure-campaign-recovery:{op_id}",
            triggered_by="manual_disclosure_campaign_recovery",
            listing_id=state.listing_id,
            request_json=request,
            max_attempts=1,
        )
        return run.id


async def run_disclosure_campaign_recovery_once(
    *,
    run_id: int,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    recovery_approval_ref: str,
    settings: Settings,
    worker_id: str = "manual-disclosure-campaign-recovery",
) -> int | None:
    """Run one explicitly queued exact-poll recovery-only native job."""
    if type(run_id) is not int or run_id < 1:
        raise InvestorError("invalid_reference", "exact recovery run id must be positive")
    async with session_scope() as session:
        request = await _assert_recovery_run(
            session,
            run_id=run_id,
            campaign_id=campaign_id,
            action_id=action_id,
            grant_id=grant_id,
            grant_revision=grant_revision,
            poll_id=poll_id,
            sample_env_id=sample_env_id,
            recovery_approval_ref=recovery_approval_ref,
        )
        if request.get("recovery_only") is not True or request.get("allow_network") is not False:
            raise InvestorError("disclosure_campaign_recovery_forbidden", "run is not recovery-only")
    handler = DisclosurePollingHandler(settings=settings)
    handler._install_campaign_dispatch_capability(_campaign_dispatch_capability(
        run_id=run_id,
        poll_id=poll_id,
        campaign_id=campaign_id,
        action_id=action_id,
        sample_env_id=sample_env_id,
        allow_network=False,
    ))
    worker = ResearchWorker(
        settings=settings,
        worker_id=worker_id,
        handlers=[handler],
        kinds=[DISCLOSURE_POLL_KIND],
    )
    return await worker.run_once(run_id=run_id)


async def _assert_recovery_run(
    session: Any,
    *,
    run_id: int,
    campaign_id: UUID,
    action_id: str,
    grant_id: UUID,
    grant_revision: int,
    poll_id: UUID,
    sample_env_id: str,
    recovery_approval_ref: str,
) -> dict[str, Any]:
    from investment_agent.supplemental_rules.disclosure_campaign import authorize_campaign_recovery

    run = await session.get(PipelineRun, run_id)
    request = dict(run.request_json or {}) if run is not None else {}
    if (
        run is None
        or run.kind != DISCLOSURE_POLL_KIND
        or request.get("record_type") != "poll_recovery_v1"
        or str(request.get("poll_id")) != str(poll_id)
        or request.get("campaign_id") != str(campaign_id)
        or request.get("campaign_action_id") != action_id
        or request.get("grant_id") != str(grant_id)
        or request.get("grant_revision") != grant_revision
        or request.get("sample_env_id") != sample_env_id
        or request.get("recovery_approval_ref") != recovery_approval_ref
    ):
        raise InvestorError("disclosure_campaign_run_mismatch", "exact recovery run binding differs")
    operation_id = request.get("operation_id")
    decision = await authorize_campaign_recovery(
        session,
        campaign_id=campaign_id,
        poll_id=str(poll_id),
        action_id=action_id,
        grant_id=grant_id,
        grant_revision=grant_revision,
        sample_env_id=sample_env_id,
        recovery_approval_ref=recovery_approval_ref,
        operation_id=UUID(str(operation_id)),
    )
    if decision.get("recovery_only") is not True or decision.get("allow_network") is not False:
        raise InvestorError("disclosure_campaign_recovery_forbidden", "recovery-only approval is absent")
    return request
