"""Owner- and receipt-checked read models for P2 pipeline runs."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import Contract, ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.supplemental_rules.jobs import (
    CheckpointMember,
    RunCheckpoint,
    _verify_checkpoint,
    authorize_supplemental_run,
)
from investment_agent.supplemental_rules.read_snapshot import ensure_read_snapshot


class SupplementalRunMember(Contract):
    ordinal: int = Field(ge=0)
    listing_ref: ObjectRef
    context: str
    rule_ref: ObjectRef
    state: Literal["pending", "saved", "item_failed"]
    application_ref: ObjectRef | None = None
    error_code: str | None = None


class SupplementalRunView(Contract):
    run_ref: ObjectRef
    operation_id: str
    status: str
    stage: str
    manifest_hash: str
    declared_count: int
    scope_listing_refs: tuple[ObjectRef, ...]
    contexts: tuple[str, ...]
    scan_diagnostics: tuple[str, ...]
    unbound_event_refs: tuple[ObjectRef, ...]
    coverage_complete: bool
    completed_count: int
    failed_items: tuple[dict[str, Any], ...]
    members: tuple[SupplementalRunMember, ...]
    next_cursor: str | None = None
    observed_at: datetime
    failure_code: Literal["writes_disabled", "item_failures", "evaluation_failed"] | None = None
    error_summary: str | None = None


def _checkpoint(metadata: Any, manifest_hash: str, pair_count: int) -> RunCheckpoint:
    raw = metadata.get("supplemental_checkpoint") if isinstance(metadata, dict) else None
    if raw is None:
        return RunCheckpoint(
            source_manifest_hash=manifest_hash,
            members=tuple(CheckpointMember(ordinal=i, state="pending") for i in range(pair_count)),
            updated_at=datetime.now(UTC),
        )
    try:
        checkpoint = RunCheckpoint.model_validate(raw)
    except ValidationError as exc:
        raise InvestorError(
            "historical_manifest_unavailable", "补充事项运行检查点无效"
        ) from exc
    if checkpoint.source_manifest_hash != manifest_hash or tuple(
        member.ordinal for member in checkpoint.members
    ) != tuple(range(pair_count)):
        raise InvestorError("historical_manifest_unavailable", "补充事项运行检查点不完整")
    return checkpoint


def _encode_cursor(run_id: int, manifest_hash: str, after_ordinal: int) -> str:
    raw = json.dumps(
        {"v": 1, "run_id": run_id, "manifest_hash": manifest_hash, "after": after_ordinal},
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, run_id: int, manifest_hash: str, pair_count: int) -> int:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding).decode())
        if (
            value.get("v") != 1
            or value.get("run_id") != run_id
            or value.get("manifest_hash") != manifest_hash
        ):
            raise InvestorError("revision_conflict", "评估批次分页快照不匹配")
        after = value.get("after")
        if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= pair_count:
            raise ValueError("invalid ordinal")
        return after
    except InvestorError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise InvestorError("invalid_cursor", "评估批次分页标识无效") from exc


async def supplemental_run_view(
    session: AsyncSession,
    run_id: int,
    *,
    owner_id: OwnerId = "local_owner",
    cursor: str | None = None,
    limit: int = 100,
) -> SupplementalRunView:
    """Return one fixed-ordinal page without exposing request_json/manifest."""
    if not 1 <= limit <= 100:
        raise InvestorError("invalid_cursor", "分页大小须为1至100")
    await ensure_read_snapshot(session)
    run, authorized = await authorize_supplemental_run(session, run_id, owner_id=owner_id)
    try:
        checkpoint = _checkpoint(
            run.metadata_json, authorized.manifest.manifest_hash, len(authorized.manifest.pairs)
        )
        await _verify_checkpoint(session, checkpoint, authorized)
        states = tuple(member.state for member in checkpoint.members)
        if run.status == "success" and any(state != "saved" for state in states):
            raise ValueError("successful run has an incomplete checkpoint")
        if run.status == "partial" and (
            any(state == "pending" for state in states)
            or not any(state == "item_failed" for state in states)
        ):
            raise ValueError("partial run has an incomplete failure checkpoint")
    except InvestorError:
        raise
    except (ValueError, ValidationError) as exc:
        raise InvestorError(
            "historical_manifest_unavailable", "补充事项运行检查点与冻结应用不匹配"
        ) from exc
    after = (
        _decode_cursor(cursor, run_id, authorized.manifest.manifest_hash, len(checkpoint.members))
        if cursor
        else 0
    )
    pairs = authorized.manifest.pairs[after : after + limit]
    members = tuple(
        SupplementalRunMember(
            ordinal=pair.ordinal,
            listing_ref=pair.listing_ref,
            context=pair.context,
            rule_ref=pair.rule_ref,
            state=checkpoint.members[pair.ordinal].state,
            application_ref=checkpoint.members[pair.ordinal].application_ref,
            error_code=checkpoint.members[pair.ordinal].error_code,
        )
        for pair in pairs
    )
    next_after = after + len(pairs)
    return SupplementalRunView(
        run_ref=authorized.run_ref,
        operation_id=str(authorized.operation_id),
        status=run.status,
        stage=run.stage,
        manifest_hash=authorized.manifest.manifest_hash,
        declared_count=len(authorized.manifest.pairs),
        scope_listing_refs=authorized.manifest.listing_refs,
        contexts=tuple(sorted(authorized.manifest.contexts)),
        scan_diagnostics=authorized.manifest.scan_diagnostics,
        unbound_event_refs=tuple(
            ObjectRef(kind="research_event", id=event.id, revision=event.revision)
            for event in authorized.manifest.unbound_events
        ),
        coverage_complete=(
            run.status == "success"
            and bool(authorized.manifest.pairs)
            and all(member.state == "saved" for member in checkpoint.members)
            and not authorized.manifest.scan_diagnostics
            and not authorized.manifest.unbound_events
        ),
        completed_count=sum(member.state == "saved" for member in checkpoint.members),
        failed_items=tuple(
            {"ordinal": member.ordinal, "reason_code": member.error_code}
            for member in checkpoint.members
            if member.state == "item_failed"
        ),
        members=members,
        next_cursor=(
            _encode_cursor(run_id, authorized.manifest.manifest_hash, next_after)
            if next_after < len(checkpoint.members)
            else None
        ),
        observed_at=datetime.now(UTC),
        failure_code=(
            "writes_disabled"
            if run.error_summary == "writes_disabled"
            else "item_failures"
            if run.status == "partial"
            else "evaluation_failed"
            if run.status == "failed"
            else None
        ),
        error_summary=run.error_summary,
    )
