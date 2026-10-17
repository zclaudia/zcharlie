"""One-shot raw disclosure source probe isolated from normal evidence ingestion.

Probe output is deliberately limited to a quarantine sink and an audit ledger.
It cannot construct page, original, candidate, event, coverage, or V3 proof rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.engine import make_url

from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    MAX_BYTES,
    QUERY,
    DisclosureError,
    OfficialHTTPS,
    SourceResponse,
    validate_url,
)
from investment_agent.evidence.disclosure_profiles import (
    canonical_hash,
)
from investment_agent.investor.common import Contract

Purpose = Literal["terms", "directory", "query", "original"]

PURPOSE_BYTES: dict[Purpose, int] = {
    "terms": 1024 * 1024,
    "directory": 8 * 1024 * 1024,
    "query": 2 * 1024 * 1024,
    "original": MAX_BYTES,
}
PURPOSE_HOPS: dict[Purpose, int] = {"terms": 2, "directory": 1, "query": 4, "original": 1}
TOTAL_HOPS = 8
TOTAL_RESPONSE_BYTES = 40 * 1024 * 1024


def current_probe_build() -> str:
    package_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for filename in ("supplemental_rules/disclosure_probe.py", "evidence/disclosure_http.py"):
        digest.update(filename.encode() + b"\0")
        digest.update((package_root / filename).read_bytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class ProbeRequest:
    purpose: Purpose
    url: str
    body: bytes | None = None
    max_bytes: int = 0


class ProbeOperationScope(Contract):
    campaign_action_id: str = Field(min_length=1, max_length=128)
    purpose: Purpose
    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST"]
    request_body_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_response_bytes: int = Field(ge=1, le=MAX_BYTES)
    max_storage_bytes: int = Field(ge=1, le=17 * 1024 * 1024)
    redirect_depth: int | None = Field(default=None, ge=1, le=2)
    previous_operation_index: int | None = Field(default=None, ge=0, le=7)
    indexed_by_operation_index: int | None = Field(default=None, ge=0, le=7)
    indexed_native_id: str | None = Field(default=None, pattern=r"^\d{1,32}$")
    indexed_native_id_field: str | None = Field(default=None, min_length=1, max_length=80)
    indexed_locator_field: str | None = Field(default=None, min_length=1, max_length=80)
    indexed_locator_value: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def method_and_body_match(self) -> ProbeOperationScope:
        if (self.method == "GET") != (self.request_body_sha256 is None):
            raise ValueError("probe operation method/body hash mismatch")
        if self.redirect_depth is not None and self.redirect_depth > 2:
            raise ValueError("probe redirect depth exceeds two hops")
        if self.max_storage_bytes < self.max_response_bytes + 1024 * 1024:
            raise ValueError("probe storage cap must include raw body and bounded metadata")
        index_values = (
            self.indexed_by_operation_index, self.indexed_native_id,
            self.indexed_native_id_field, self.indexed_locator_field, self.indexed_locator_value,
        )
        if self.purpose == "original":
            if any(value is None for value in index_values):
                raise ValueError("S0 original must bind an exact prior index locator")
            if self.indexed_locator_value and not self.indexed_locator_value.endswith(
                f"{self.indexed_native_id}.PDF"
            ):
                raise ValueError("S0 original locator must preserve the indexed native id")
        elif any(value is not None for value in index_values):
            raise ValueError("only an original request may bind an index locator")
        return self


class DisclosureProbeApprovalV1(Contract):
    schema_version: Literal[1] = 1
    campaign_id: UUID
    sample_env_id: str = Field(min_length=1, max_length=128)
    approval_ref: str = Field(min_length=1, max_length=256)
    executor_build: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_dir: str = Field(min_length=1, max_length=2048)
    quarantine_dir: str = Field(min_length=1, max_length=2048)
    evidence_hashes: tuple[str, ...] = Field(min_length=1, max_length=16)
    executor_name: str = Field(min_length=1, max_length=200)
    reviewer_name: str = Field(min_length=1, max_length=200)
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    operations: tuple[ProbeOperationScope, ...] = Field(min_length=1, max_length=TOTAL_HOPS)
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def immutable_scope(self) -> DisclosureProbeApprovalV1:
        if self.executor_name.strip().casefold() == self.reviewer_name.strip().casefold():
            raise ValueError("probe reviewer must be independent from executor")
        if self.valid_from >= self.expires_at:
            raise ValueError("probe approval window must be positive")
        if self.executor_build != current_probe_build():
            raise ValueError("probe approval executor build is stale or not server-derived")
        quarantine_path = Path(self.quarantine_dir)
        data_path = Path(self.data_dir)
        if (
            not quarantine_path.is_absolute()
            or not data_path.is_absolute()
            or ".." in quarantine_path.parts
            or ".." in data_path.parts
            or quarantine_path == data_path
            or not quarantine_path.is_relative_to(data_path)
        ):
            raise ValueError("probe quarantine path must be an exact absolute path")
        if len(set(self.evidence_hashes)) != len(self.evidence_hashes) or any(
            len(item) != 64 or any(c not in "0123456789abcdef" for c in item)
            for item in self.evidence_hashes
        ):
            raise ValueError("probe evidence hashes must be unique SHA-256 values")
        if self.approval_hash != probe_approval_hash(self):
            raise ValueError("probe approval hash mismatch")
        exact_terms = tuple(item.url for item in self.operations if item.purpose == "terms")
        if len(set(exact_terms)) > 2:
            raise ValueError("probe approval permits at most two exact terms URLs")
        if len(exact_terms) != len(set(exact_terms)):
            raise ValueError("each exact terms URL may be fetched once")
        if sum(item.max_response_bytes for item in self.operations) > TOTAL_RESPONSE_BYTES:
            raise ValueError("probe approval exceeds S0 response budget")
        if sum(item.max_storage_bytes for item in self.operations) > 128 * 1024 * 1024:
            raise ValueError("probe approval exceeds S0 storage budget")
        if len({item.campaign_action_id for item in self.operations}) != len(self.operations):
            raise ValueError("each probe operation requires its own campaign action")
        if any(item.redirect_depth is not None and index == 0 for index, item in enumerate(self.operations)):
            raise ValueError("first probe operation cannot be a redirect hop")
        counts: dict[str, int] = {}
        for item in self.operations:
            counts[item.purpose] = counts.get(item.purpose, 0) + 1
            if counts[item.purpose] > PURPOSE_HOPS[item.purpose]:
                raise ValueError("probe approval exceeds purpose hop cap")
        for operation_index, item in enumerate(self.operations):
            expected_previous = operation_index - 1 if operation_index else None
            if item.previous_operation_index != expected_previous:
                raise ValueError("probe operations must use one ordered, non-skippable sequence")
            try:
                validate_url(item.url, item.purpose, exact_urls=exact_terms)
            except DisclosureError as exc:
                raise ValueError("probe operation URL is outside official allowlist") from exc
            if item.purpose == "original":
                index = item.indexed_by_operation_index
                if index is None or index >= len(self.operations) or index >= operation_index:
                    raise ValueError("S0 original index operation must precede the PDF request")
                if self.operations[index].purpose != "query":
                    raise ValueError("S0 original must bind a query index response")
                normalized = DisclosureProbeExecutor._normalize_index_locator(item.indexed_locator_value or "")
                if normalized != item.url:
                    raise ValueError("S0 original URL must equal its exact index locator")
        return self


def _approval_material(value: DisclosureProbeApprovalV1 | dict[str, Any]) -> dict[str, Any]:
    material = value.model_dump(mode="json", exclude={"approval_hash"}) if isinstance(
        value, DisclosureProbeApprovalV1
    ) else {key: item for key, item in value.items() if key != "approval_hash"}
    return material


def probe_approval_hash(value: DisclosureProbeApprovalV1 | dict[str, Any]) -> str:
    return canonical_hash(_approval_material(value))


class ProbeApprovalRegistry:
    """Read-only for executors; explicit server installation and revocation only."""

    def __init__(self) -> None:
        self._approvals: dict[str, tuple[DisclosureProbeApprovalV1, datetime | None, str | None]] = {}
        self._lock = RLock()

    @classmethod
    def for_tests(cls) -> ProbeApprovalRegistry:
        return cls()

    def install_approved(self, approval: DisclosureProbeApprovalV1) -> None:
        with self._lock:
            existing = self._approvals.get(approval.approval_hash)
            if existing is not None:
                if existing[0] != approval:
                    raise DisclosureError("probe_approval_hash_collision")
                return
            self._approvals[approval.approval_hash] = (approval, None, None)

    def revoke(self, approval_hash: str, *, at: datetime, reference: str) -> None:
        with self._lock:
            entry = self._approvals.get(approval_hash)
            if entry is None:
                raise DisclosureError("probe_approval_unknown")
            if entry[1] is not None:
                if entry[1] == at.astimezone(UTC) and entry[2] == reference:
                    return
                raise DisclosureError("probe_approval_revocation_conflict")
            self._approvals[approval_hash] = (entry[0], at.astimezone(UTC), reference)

    def require(self, approval_hash: str, *, now: datetime) -> DisclosureProbeApprovalV1:
        with self._lock:
            entry = self._approvals.get(approval_hash)
        if entry is None or not entry[0].valid_from <= now < entry[0].expires_at:
            raise DisclosureError("probe_approval_inactive")
        if entry[1] is not None and now >= entry[1]:
            raise DisclosureError("probe_approval_revoked")
        if entry[0].executor_build != current_probe_build():
            raise DisclosureError("probe_approval_executor_build_stale")
        return entry[0]

    def lookup(self, approval_hash: str) -> tuple[DisclosureProbeApprovalV1, datetime | None, str | None] | None:
        """Read approval metadata for progress reporting, even after expiry/revocation."""
        with self._lock:
            return self._approvals.get(approval_hash)


SERVER_PROBE_APPROVAL_REGISTRY = ProbeApprovalRegistry()


@dataclass(frozen=True)
class QuarantinedProbeCapture:
    approval_hash: str
    campaign_id: str
    campaign_action_id: str
    operation_key: str
    permit_id: str
    captured_at: datetime
    purpose: Purpose
    request_url: str
    request_body_sha256: str | None
    status: int | None
    response_url: str | None
    response_headers: tuple[tuple[str, str], ...]
    response_body: bytes
    response_sha256: str
    response_bytes: int
    outcome: Literal["complete", "terminal_error"]
    failure_reason: str | None


class ProbeLedger(Protocol):
    """Campaign ledger methods; implementations must persist every state change."""

    def require_file_backed(self) -> None: ...

    async def reserve(
        self,
        operation_key: str,
        purpose: str,
        url: str,
        request_bytes: int,
        max_response_bytes: int,
        *,
        campaign_action_id: str,
        campaign_id: UUID,
        sample_env_id: str,
        max_storage_bytes: int,
        body_hash: str | None,
    ) -> Any: ...

    async def mark_sent(self, permit_ref: Any) -> None: ...

    async def settle(
        self, permit_ref: Any, *, outcome: str, response_bytes: int, response_hash: str | None
    ) -> None: ...

    async def cancel(self, permit_ref: Any, *, reason: str) -> None: ...


class QuarantineSink(Protocol):
    def append_raw(self, capture: QuarantinedProbeCapture) -> None: ...

    def read_audit(self, approval_hash: str, operation_key: str) -> dict[str, Any] | None: ...

    def read_capture(self, approval_hash: str, operation_key: str) -> tuple[dict[str, Any], bytes] | None: ...


class AtomicDirectoryQuarantineSink:
    """Append-only raw/audit pair in a pre-created private temporary directory."""

    def __init__(self, path: str | Path, *, max_bytes: int = 128 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        if not self.path.is_absolute() or self.path.is_symlink() or not self.path.is_dir():
            raise DisclosureError("probe_quarantine_directory_invalid")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & 0o077:
            raise DisclosureError("probe_quarantine_permissions_invalid")
        self._used_bytes = 0
        for item in self.path.iterdir():
            if item.is_symlink() or not item.is_file():
                raise DisclosureError("probe_quarantine_contents_invalid")
            self._used_bytes += item.stat().st_size
        if self._used_bytes > self.max_bytes:
            raise DisclosureError("probe_quarantine_capacity_exceeded")

    def append_raw(self, capture: QuarantinedProbeCapture) -> None:
        stem = f"{capture.approval_hash}-{capture.operation_key}"
        raw_path = self.path / f"{stem}.raw"
        audit_path = self.path / f"{stem}.json"
        audit = json.dumps(
            {
                "schema_version": 1,
                "approval_hash": capture.approval_hash,
                "campaign_id": capture.campaign_id,
                "campaign_action_id": capture.campaign_action_id,
                "operation_key": capture.operation_key,
                "permit_id": capture.permit_id,
                "captured_at": capture.captured_at.isoformat(),
                "purpose": capture.purpose,
                "request_url": capture.request_url,
                "request_body_sha256": capture.request_body_sha256,
                "status": capture.status,
                "response_url": capture.response_url,
                "response_headers": dict(capture.response_headers),
                "response_body_sha256": capture.response_sha256,
                "response_bytes": capture.response_bytes,
                "outcome": capture.outcome,
                "failure_reason": capture.failure_reason,
                "raw_file": raw_path.name,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        additional = len(capture.response_body) + len(audit)
        if self._used_bytes + additional > self.max_bytes:
            raise DisclosureError("probe_quarantine_capacity_exceeded")
        try:
            self._exclusive_write(raw_path, capture.response_body)
            self._exclusive_write(audit_path, audit)
        except BaseException:
            # Preserve any created half-pair for audit; never overwrite it.
            raise
        self._used_bytes += additional

    def read_capture(self, approval_hash: str, operation_key: str) -> tuple[dict[str, Any], bytes] | None:
        audit = self.read_audit(approval_hash, operation_key)
        if audit is None:
            return None
        stem = f"{approval_hash}-{operation_key}"
        raw_path = self.path / f"{stem}.raw"
        if not raw_path.exists():
            return None
        if raw_path.is_symlink():
            raise DisclosureError("probe_quarantine_contents_invalid")
        body = raw_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != audit.get("response_body_sha256"):
            raise DisclosureError("probe_quarantine_hash_mismatch")
        return audit, body

    def read_audit(self, approval_hash: str, operation_key: str) -> dict[str, Any] | None:
        stem = f"{approval_hash}-{operation_key}"
        audit_path = self.path / f"{stem}.json"
        if not audit_path.exists():
            return None
        if audit_path.is_symlink():
            raise DisclosureError("probe_quarantine_contents_invalid")
        value = json.loads(audit_path.read_text())
        if not isinstance(value, dict):
            raise DisclosureError("probe_quarantine_audit_invalid")
        if value.get("approval_hash") != approval_hash or value.get("operation_key") != operation_key:
            raise DisclosureError("probe_quarantine_identity_mismatch")
        return value

    @staticmethod
    def _exclusive_write(path: Path, body: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())


class CampaignProbeLedger:
    """Async SQLite adapter to the campaign's existing immutable hop journal."""

    def __init__(self, *, session_factory: Any) -> None:
        self.session_factory = session_factory

    def require_file_backed(self) -> None:
        """Reject volatile or URI SQLite stores at the live raw-send boundary."""
        bind = getattr(self.session_factory, "kw", {}).get("bind")
        url_value = getattr(bind, "url", None)
        if url_value is None:
            raise DisclosureError("probe_ledger_requires_file_sqlite")
        try:
            url = make_url(str(url_value))
        except (TypeError, ValueError) as exc:
            raise DisclosureError("probe_ledger_requires_file_sqlite") from exc
        database = url.database
        if (
            url.get_backend_name() != "sqlite"
            or not database
            or database == ":memory:"
            or database.startswith("file:")
            or "mode" in url.query
        ):
            raise DisclosureError("probe_ledger_requires_file_sqlite")

    async def recover_hop(
        self,
        *,
        permit_id: UUID,
        campaign_id: UUID,
        sample_env_id: str,
        operation_id: UUID,
        recovery_approval_ref: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Resolve one exact orphan permit as canceled or charged unknown.

        Recovery is accounting-only: it has no approval-registry, capture,
        HTTP, or persistence-sink dependency and never authorizes retransmit.
        This narrow server-side entrypoint is for an explicitly reviewed
        operator command; operation_id and recovery_approval_ref are recorded
        as its immutable decision identity, not independently verified proof
        of the operator's human approval.
        """
        self.require_file_backed()
        from investment_agent.supplemental_rules.disclosure_probe_quota import (
            recover_raw_probe_hop,
        )

        async with self.session_factory() as session, session.begin():
            return await recover_raw_probe_hop(
                session,
                permit_id=permit_id,
                campaign_id=campaign_id,
                sample_env_id=sample_env_id,
                operation_id=operation_id,
                approval_ref=recovery_approval_ref,
                now=now or datetime.now(UTC),
            )

    async def reserve(
        self,
        operation_key: str,
        purpose: str,
        url: str,
        request_bytes: int,
        max_response_bytes: int,
        *,
        campaign_id: UUID,
        campaign_action_id: str,
        sample_env_id: str,
        max_storage_bytes: int,
        body_hash: str | None,
    ) -> dict[str, Any]:
        from investment_agent.supplemental_rules.disclosure_campaign import (
            reserve_campaign_action_hop,
        )

        campaign_purpose: Literal["terms", "raw_probe"] = "terms" if purpose == "terms" else "raw_probe"
        async with self.session_factory() as session, session.begin():
            return await reserve_campaign_action_hop(
                session,
                campaign_id=campaign_id,
                action_id=campaign_action_id,
                operation_key=operation_key,
                sample_env_id=sample_env_id,
                purpose=campaign_purpose,
                url=url,
                request_bytes=request_bytes,
                response_bytes_limit=max_response_bytes,
                storage_bytes_limit=max_storage_bytes,
                body_hash=body_hash,
            )

    async def operation_status(
        self, approval: DisclosureProbeApprovalV1, operation_index: int,
    ) -> dict[str, Any]:
        from investment_agent.storage.repositories.supplemental_updates import (
            SupplementalUpdatesRepository,
        )
        from investment_agent.supplemental_rules.disclosure_campaign import (
            OWNER,
            read_campaign_hop_quarantine,
        )

        scope = approval.operations[operation_index]
        operation_key = DisclosureProbeExecutor._operation_key(
            approval.approval_hash, operation_index, scope,
        )
        permit_id = uuid5(
            NAMESPACE_URL,
            f"p4-campaign:{approval.campaign_id}:s0:{scope.campaign_action_id}:{operation_key}",
        )
        async with self.session_factory() as session:
            repo = SupplementalUpdatesRepository(session)
            prefix = f"p4:campaign-hop:{permit_id}"
            reservation = await repo.get_change_by_key(OWNER, f"{prefix}:reserved")
            sent = await repo.get_change_by_key(OWNER, f"{prefix}:sent")
            result = await repo.get_change_by_key(OWNER, f"{prefix}:result")
            canceled = await repo.get_change_by_key(OWNER, f"{prefix}:canceled")
            quarantine = await read_campaign_hop_quarantine(session, permit_id=permit_id)
            state = (
                "stored" if quarantine["state"] == "stored" else
                "failed" if quarantine["state"] == "failed" else
                "unknown" if result and result.payload.get("stage") == "unknown" else
                "pending" if quarantine["state"] == "pending" else
                "settled" if result else "sent" if sent else
                "reserved" if reservation else "not_started"
            )
            return {"operation_index": operation_index, "operation_key": operation_key,
                    "permit_id": str(permit_id), "state": state,
                    "quarantine_state": quarantine["state"], "sent": sent is not None,
                    "terminal": bool(result or canceled)}

    async def assert_current(
        self, permit_ref: dict[str, Any], *, campaign_id: UUID, action_id: str,
        sample_env_id: str,
    ) -> None:
        """Re-read the current campaign/action after bytes arrive, before raw persistence."""
        from investment_agent.supplemental_rules.disclosure_campaign import (
            assert_campaign_hop_current,
        )

        async with self.session_factory() as session:
            try:
                await assert_campaign_hop_current(
                    session, permit_id=UUID(permit_ref["permit_id"]),
                    sample_env_id=sample_env_id, now=datetime.now(UTC),
                )
            except Exception as exc:
                raise DisclosureError("probe_campaign_authority_changed") from exc

    async def record_quarantine(self, permit_ref: dict[str, Any], *, outcome: str, reason: str) -> None:
        """Persist raw_pending/stored metadata without copying response content."""
        from investment_agent.supplemental_rules.disclosure_campaign import (
            record_campaign_hop_quarantine,
        )

        async with self.session_factory() as session, session.begin():
            await record_campaign_hop_quarantine(
                session,
                permit_id=UUID(permit_ref["permit_id"]),
                status="stored" if outcome == "stored" else "failed",
                reason=f"{outcome}:{reason}"[:256],
            )

    async def mark_sent(self, permit_ref: dict[str, Any]) -> None:
        from investment_agent.supplemental_rules.disclosure_campaign import mark_campaign_hop_sent

        async with self.session_factory() as session, session.begin():
            await mark_campaign_hop_sent(session, permit_id=UUID(permit_ref["permit_id"]))

    async def settle(
        self,
        permit_ref: dict[str, Any],
        *,
        outcome: str,
        response_bytes: int,
        response_hash: str | None,
    ) -> None:
        from investment_agent.supplemental_rules.disclosure_campaign import settle_campaign_hop

        async with self.session_factory() as session, session.begin():
            await settle_campaign_hop(
                session,
                permit_id=UUID(permit_ref["permit_id"]),
                response_bytes=None if outcome == "unknown" else response_bytes,
                storage_bytes=permit_ref["storage_bytes"] if outcome == "unknown" else response_bytes + 1024 * 1024,
                result_code=f"{outcome}:{response_hash or 'unknown'}",
            )

    async def cancel(self, permit_ref: dict[str, Any], *, reason: str) -> None:
        from investment_agent.supplemental_rules.disclosure_campaign import cancel_campaign_hop

        # Cancellation is called only before the HTTP client is invoked.
        async with self.session_factory() as session, session.begin():
            await cancel_campaign_hop(session, permit_id=UUID(permit_ref["permit_id"]))


class DisclosureProbeExecutor:
    """Capture explicitly enumerated raw requests through a campaign ledger."""

    def __init__(
        self,
        *,
        ledger: ProbeLedger,
        quarantine: QuarantineSink,
        registry: ProbeApprovalRegistry | None = None,
        http: Any | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self.ledger = ledger
        self.quarantine = quarantine
        self.registry = registry if registry is not None else SERVER_PROBE_APPROVAL_REGISTRY
        self.http = http if http is not None else OfficialHTTPS()
        self.data_dir = Path(data_dir) if data_dir is not None else None
        if type(self) is DisclosureProbeExecutor and (
            self.registry is not SERVER_PROBE_APPROVAL_REGISTRY
            or not isinstance(self.http, OfficialHTTPS)
            or not isinstance(self.ledger, CampaignProbeLedger)
            or not isinstance(self.quarantine, AtomicDirectoryQuarantineSink)
            or self.data_dir is None
            or not self.data_dir.is_absolute()
        ):
            raise DisclosureError("probe_live_dependencies_must_be_server_owned")
        if type(self) is DisclosureProbeExecutor:
            self.ledger.require_file_backed()

    @classmethod
    def for_tests(
        cls, *, ledger: ProbeLedger, quarantine: QuarantineSink, http: Any,
        registry: ProbeApprovalRegistry | None = None,
    ) -> DisclosureProbeExecutor:
        instance = object.__new__(_TestDisclosureProbeExecutor)
        instance.ledger = ledger
        instance.quarantine = quarantine
        instance.registry = registry or ProbeApprovalRegistry.for_tests()
        instance.http = http
        return instance

    async def capture(
        self,
        request: ProbeRequest,
        *,
        operation_index: int,
        approval_hash: str,
        now: datetime,
    ) -> QuarantinedProbeCapture:
        approval = self.registry.require(approval_hash, now=now)
        is_test_executor = type(self) is _TestDisclosureProbeExecutor
        if not is_test_executor and (
            self.registry is not SERVER_PROBE_APPROVAL_REGISTRY
            or not isinstance(self.http, OfficialHTTPS)
            or not isinstance(self.ledger, CampaignProbeLedger)
            or not isinstance(self.quarantine, AtomicDirectoryQuarantineSink)
            or self.data_dir is None
            or str(self.data_dir) != approval.data_dir
            or str(self.quarantine.path) != approval.quarantine_dir
        ):
            raise DisclosureError("probe_live_dependencies_must_be_server_owned")
        if not is_test_executor:
            self.ledger.require_file_backed()
        current_now = now if is_test_executor else datetime.now(UTC)
        if not approval.valid_from <= current_now < approval.expires_at:
            raise DisclosureError("probe_approval_inactive")
        if not 0 <= operation_index < len(approval.operations):
            raise DisclosureError("probe_operation_index_invalid")
        scope = approval.operations[operation_index]
        self._check_request(request, scope, approval.operations)
        await self._verify_previous_operation(
            approval_hash, approval.operations, operation_index, scope, current_now
        )
        if scope.redirect_depth is not None:
            await self._verify_redirect_predecessor(
                approval_hash, approval.operations, operation_index, scope, current_now
            )
        if scope.purpose == "original":
            await self._verify_indexed_original(
                approval_hash, approval.operations, scope, current_now
            )
        if self.registry.require(approval_hash, now=current_now) != approval:
            raise DisclosureError("probe_approval_changed")
        request_hash = hashlib.sha256(request.body).hexdigest() if request.body is not None else None
        op_key = hashlib.sha256(
            f"{approval_hash}:{operation_index}:{request.purpose}:{request.url}:{request_hash}".encode()
        ).hexdigest()
        existing_audit = self.quarantine.read_audit(approval_hash, op_key)
        if existing_audit is not None:
            await self._assert_capture_current(existing_audit, approval.sample_env_id)
            raise DisclosureError("probe_operation_already_captured")
        permit = await self.ledger.reserve(
            op_key, request.purpose, request.url, len(request.body or b""), request.max_bytes,
            campaign_action_id=scope.campaign_action_id,
            campaign_id=approval.campaign_id,
            sample_env_id=approval.sample_env_id,
            max_storage_bytes=scope.max_storage_bytes,
            body_hash=request_hash,
        )
        try:
            pre_send_now = now if is_test_executor else datetime.now(UTC)
            self.registry.require(approval_hash, now=pre_send_now)
            assert_current = getattr(self.ledger, "assert_current", None)
            if assert_current is not None:
                await assert_current(
                    permit, campaign_id=approval.campaign_id,
                    action_id=scope.campaign_action_id,
                    sample_env_id=approval.sample_env_id,
                )
            await self.ledger.mark_sent(permit)
        except BaseException as exc:
            await self.ledger.cancel(permit, reason="proved_not_sent")
            raise DisclosureError("probe_authority_changed_before_send") from exc
        try:
            self.registry.require(
                approval_hash, now=now if is_test_executor else datetime.now(UTC)
            )
            assert_current = getattr(self.ledger, "assert_current", None)
            if assert_current is not None:
                await assert_current(
                    permit, campaign_id=approval.campaign_id,
                    action_id=scope.campaign_action_id,
                    sample_env_id=approval.sample_env_id,
                )
        except BaseException as exc:
            await self.ledger.settle(
                permit, outcome="unknown", response_bytes=request.max_bytes, response_hash=None
            )
            record_failure = getattr(self.ledger, "record_quarantine", None)
            if record_failure is not None:
                await record_failure(permit, outcome="withheld", reason="authority_changed_before_http")
            raise DisclosureError("probe_authority_changed_before_http") from exc
        try:
            if isinstance(self.http, OfficialHTTPS):
                response = await self.http.request_one_hop(
                    request.url, request.purpose, body=request.body, max_bytes=request.max_bytes,
                    exact_urls=tuple(item.url for item in approval.operations if item.purpose == "terms"),
                )
            else:
                response = await self.http.request_hop(
                    request.url, request.purpose, body=request.body, max_bytes=request.max_bytes,
                )
        except BaseException as exc:
            await self.ledger.settle(
                permit, outcome="unknown", response_bytes=request.max_bytes, response_hash=None
            )
            record_failure = getattr(self.ledger, "record_quarantine", None)
            if record_failure is not None:
                await record_failure(
                    permit, outcome="failed", reason=f"http_error:{type(exc).__name__}"
                )
            raise

        digest = hashlib.sha256(response.body).hexdigest()
        failure_reason = self._response_failure(request.purpose, response)
        capture = QuarantinedProbeCapture(
            approval_hash=approval_hash,
            campaign_id=str(approval.campaign_id),
            campaign_action_id=scope.campaign_action_id,
            operation_key=op_key,
            permit_id=str(permit["permit_id"]),
            captured_at=current_now,
            purpose=request.purpose,
            request_url=request.url,
            request_body_sha256=request_hash,
            status=response.status,
            response_url=response.url,
            response_headers=self._safe_headers(response.headers),
            response_body=response.body,
            response_sha256=digest,
            response_bytes=len(response.body),
            outcome="terminal_error" if failure_reason else "complete",
            failure_reason=failure_reason,
        )
        after_response_now = now if is_test_executor else datetime.now(UTC)
        try:
            self.registry.require(approval_hash, now=after_response_now)
            assert_current = getattr(self.ledger, "assert_current", None)
            if assert_current is not None:
                await assert_current(
                    permit, campaign_id=approval.campaign_id, action_id=scope.campaign_action_id,
                    sample_env_id=approval.sample_env_id,
                )
        except BaseException as exc:
            await self.ledger.settle(
                permit, outcome="withheld", response_bytes=len(response.body), response_hash=digest
            )
            record_failure = getattr(self.ledger, "record_quarantine", None)
            if record_failure is not None:
                await record_failure(permit, outcome="withheld", reason="authority_changed_after_response")
            raise DisclosureError("probe_authority_changed_after_response") from exc
        await self.ledger.settle(
            permit,
            outcome="terminal_error" if failure_reason else "complete",
            response_bytes=len(response.body),
            response_hash=digest,
        )
        try:
            assert_current = getattr(self.ledger, "assert_current", None)
            if assert_current is not None:
                await assert_current(
                    permit, campaign_id=approval.campaign_id,
                    action_id=scope.campaign_action_id,
                    sample_env_id=approval.sample_env_id,
                )
            self.registry.require(
                approval_hash, now=now if is_test_executor else datetime.now(UTC)
            )
        except BaseException as exc:
            record_failure = getattr(self.ledger, "record_quarantine", None)
            if record_failure is not None:
                await record_failure(permit, outcome="withheld", reason="authority_changed_before_quarantine")
            raise DisclosureError("probe_authority_changed_before_quarantine") from exc
        try:
            self.quarantine.append_raw(capture)
        except BaseException as exc:
            record_failure = getattr(self.ledger, "record_quarantine", None)
            if record_failure is not None:
                await record_failure(permit, outcome="failed", reason=type(exc).__name__)
            raise DisclosureError("probe_raw_pending_after_sink_failure") from exc
        record_stored = getattr(self.ledger, "record_quarantine", None)
        if record_stored is not None:
            await record_stored(permit, outcome="stored", reason=failure_reason or "complete")
        return capture

    async def read_progress(self, approval_hash: str) -> dict[str, Any]:
        record = self.registry.lookup(approval_hash)
        if record is None:
            raise DisclosureError("probe_approval_unknown")
        approval, revoked_at, _reference = record
        if not isinstance(self.ledger, CampaignProbeLedger):
            raise DisclosureError("probe_progress_requires_campaign_ledger")
        operations = tuple(
            [await self.ledger.operation_status(approval, index) for index in range(len(approval.operations))]
        )
        return {
            "approval_hash": approval_hash,
            "campaign_id": str(approval.campaign_id),
            "sample_env_id": approval.sample_env_id,
            "revoked_at": revoked_at.isoformat() if revoked_at else None,
            "operations": operations,
        }

    @staticmethod
    def _check_request(
        request: ProbeRequest,
        scope: ProbeOperationScope,
        operations: tuple[ProbeOperationScope, ...],
    ) -> None:
        method = "POST" if request.body is not None else "GET"
        body_hash = hashlib.sha256(request.body).hexdigest() if request.body is not None else None
        if (request.purpose, request.url, method, body_hash, request.max_bytes) != (
            scope.purpose, scope.url, scope.method, scope.request_body_sha256, scope.max_response_bytes
        ):
            raise DisclosureError("probe_operation_not_exactly_approved")
        if request.max_bytes != PURPOSE_BYTES[request.purpose]:
            raise DisclosureError("probe_response_cap_mismatch")
        exact_terms_urls = tuple(item.url for item in operations if item.purpose == "terms")
        validate_url(request.url, request.purpose, exact_urls=exact_terms_urls)
        if request.purpose in {"terms", "directory", "original"} and request.body is not None:
            raise DisclosureError("probe_get_body_forbidden")
        if request.purpose == "directory" and request.url != DIRECTORY:
            raise DisclosureError("probe_directory_scope_mismatch")
        if request.purpose == "query":
            if request.url != QUERY:
                raise DisclosureError("probe_query_scope_mismatch")
            DisclosureProbeExecutor._validate_fixed_query_body(request.body)

    async def _verify_redirect_predecessor(
        self,
        approval_hash: str,
        operations: tuple[ProbeOperationScope, ...],
        operation_index: int,
        scope: ProbeOperationScope,
        now: datetime,
    ) -> None:
        assert scope.redirect_depth is not None
        previous_index = operation_index - 1
        previous = operations[previous_index]
        if (
            previous.purpose != scope.purpose
            or (previous.redirect_depth or 0) != scope.redirect_depth - 1
        ):
            raise DisclosureError("probe_redirect_predecessor_mismatch")
        read_capture = getattr(self.quarantine, "read_capture", None)
        if read_capture is None:
            raise DisclosureError("probe_redirect_predecessor_unavailable")
        read_audit = getattr(self.quarantine, "read_audit", None)
        if read_audit is None:
            raise DisclosureError("probe_redirect_predecessor_unavailable")
        previous_key = self._operation_key(approval_hash, previous_index, previous)
        previous_audit = read_audit(approval_hash, previous_key)
        if previous_audit is None:
            raise DisclosureError("probe_redirect_predecessor_unavailable")
        await self._assert_capture_current(
            previous_audit, self.registry.require(approval_hash, now=now).sample_env_id
        )
        prior = read_capture(approval_hash, previous_key)
        if prior is None:
            raise DisclosureError("probe_redirect_predecessor_unavailable")
        audit, body = prior
        if audit.get("failure_reason") is not None:
            raise DisclosureError("probe_previous_operation_failed")
        if audit.get("status") not in {301, 302, 303, 307, 308}:
            raise DisclosureError("probe_expected_redirect_missing")
        headers = audit.get("response_headers")
        if not isinstance(headers, dict):
            raise DisclosureError("probe_redirect_location_missing")
        response = SourceResponse(
            int(audit["status"]),
            {str(key).lower(): str(value) for key, value in headers.items()},
            body,
            str(audit["response_url"]),
        )
        target = self._redirect_target(
            response,
            purpose=scope.purpose,
            depth=scope.redirect_depth,
            body_present=scope.method == "POST",
            exact_urls=tuple(item.url for item in operations if item.purpose == "terms"),
        )
        if target != scope.url:
            raise DisclosureError("probe_redirect_target_mismatch")

    async def _verify_previous_operation(
        self,
        approval_hash: str,
        operations: tuple[ProbeOperationScope, ...],
        operation_index: int,
        scope: ProbeOperationScope,
        now: datetime,
    ) -> None:
        if operation_index == 0:
            return
        previous_index = operation_index - 1
        previous = operations[previous_index]
        read_capture = getattr(self.quarantine, "read_capture", None)
        if read_capture is None:
            raise DisclosureError("probe_previous_operation_unavailable")
        read_audit = getattr(self.quarantine, "read_audit", None)
        if read_audit is None:
            raise DisclosureError("probe_previous_operation_unavailable")
        previous_key = self._operation_key(approval_hash, previous_index, previous)
        audit = read_audit(approval_hash, previous_key)
        if audit is None:
            raise DisclosureError("probe_previous_operation_unavailable")
        approval = self.registry.require(approval_hash, now=now)
        await self._assert_capture_current(audit, approval.sample_env_id)
        prior = read_capture(approval_hash, previous_key)
        if prior is None:
            raise DisclosureError("probe_previous_operation_unavailable")
        audit, _body = prior
        if audit.get("failure_reason") is not None:
            raise DisclosureError("probe_previous_operation_failed")
        if (
            scope.redirect_depth is None
            and (previous.redirect_depth is not None or audit.get("status") != 200)
        ):
            raise DisclosureError("probe_previous_operation_failed")

    @staticmethod
    def _capture_from_quarantine(audit: dict[str, Any], body: bytes) -> QuarantinedProbeCapture:
        if hashlib.sha256(body).hexdigest() != audit.get("response_body_sha256"):
            raise DisclosureError("probe_quarantine_hash_mismatch")
        return QuarantinedProbeCapture(
            approval_hash=audit["approval_hash"],
            campaign_id=audit["campaign_id"],
            campaign_action_id=audit["campaign_action_id"],
            operation_key=audit["operation_key"],
            permit_id=audit["permit_id"],
            captured_at=datetime.fromisoformat(audit["captured_at"]),
            purpose=audit["purpose"],
            request_url=audit["request_url"],
            request_body_sha256=audit.get("request_body_sha256"),
            status=audit.get("status"),
            response_url=audit.get("response_url"),
            response_headers=tuple(sorted(audit.get("response_headers", {}).items())),
            response_body=body,
            response_sha256=audit["response_body_sha256"],
            response_bytes=audit["response_bytes"],
            outcome=audit["outcome"],
            failure_reason=audit.get("failure_reason"),
        )

    async def _verify_indexed_original(
        self,
        approval_hash: str,
        operations: tuple[ProbeOperationScope, ...],
        scope: ProbeOperationScope,
        now: datetime,
    ) -> None:
        assert scope.indexed_by_operation_index is not None
        assert scope.indexed_native_id is not None
        assert scope.indexed_native_id_field is not None
        assert scope.indexed_locator_field is not None
        assert scope.indexed_locator_value is not None
        index = scope.indexed_by_operation_index
        index_scope = operations[index]
        read_capture = getattr(self.quarantine, "read_capture", None)
        if read_capture is None:
            raise DisclosureError("probe_index_capture_unavailable")
        read_audit = getattr(self.quarantine, "read_audit", None)
        if read_audit is None:
            raise DisclosureError("probe_index_capture_unavailable")
        index_key = self._operation_key(approval_hash, index, index_scope)
        index_audit = read_audit(approval_hash, index_key)
        if index_audit is None:
            raise DisclosureError("probe_index_capture_unavailable")
        approval = self.registry.require(approval_hash, now=now)
        await self._assert_capture_current(index_audit, approval.sample_env_id)
        prior = read_capture(approval_hash, index_key)
        if prior is None:
            raise DisclosureError("probe_index_capture_unavailable")
        audit, body = prior
        if audit.get("status") != 200 or audit.get("failure_reason") is not None:
            raise DisclosureError("probe_index_capture_not_usable")
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DisclosureError("probe_index_capture_invalid_json") from exc
        rows = value.get("announcements") if isinstance(value, dict) else None
        if not isinstance(rows, list) or len(rows) > 20:
            raise DisclosureError("probe_index_capture_invalid_rows")
        matches = [
            row for row in rows
            if isinstance(row, dict)
            and row.get(scope.indexed_native_id_field) == scope.indexed_native_id
        ]
        if len(matches) != 1 or matches[0].get(scope.indexed_locator_field) != scope.indexed_locator_value:
            raise DisclosureError("probe_original_not_bound_to_index_locator")

    async def _assert_capture_current(self, audit: dict[str, Any], sample_env_id: str) -> None:
        assert_current = getattr(self.ledger, "assert_current", None)
        if assert_current is None:
            return
        permit_ref = {
            "permit_id": audit.get("permit_id"),
            "campaign_id": audit.get("campaign_id"),
            "action_id": audit.get("campaign_action_id"),
            "operation_key": audit.get("operation_key"),
            "purpose": audit.get("purpose"),
            "url": audit.get("request_url"),
            "body_hash": audit.get("request_body_sha256"),
        }
        if not isinstance(permit_ref["permit_id"], str):
            raise DisclosureError("probe_capture_authority_unavailable")
        await assert_current(
            permit_ref,
            campaign_id=UUID(str(permit_ref["campaign_id"])),
            action_id=str(permit_ref["action_id"]),
            sample_env_id=sample_env_id,
        )

    @staticmethod
    def _operation_key(
        approval_hash: str,
        index: int,
        scope: ProbeOperationScope,
    ) -> str:
        return hashlib.sha256(
            f"{approval_hash}:{index}:{scope.purpose}:{scope.url}:{scope.request_body_sha256}".encode()
        ).hexdigest()

    @staticmethod
    def _normalize_index_locator(locator: str) -> str:
        if locator.startswith("https://"):
            normalized = locator
        elif locator.startswith("/finalpage/"):
            normalized = "https://static.cninfo.com.cn" + locator
        elif locator.startswith("finalpage/"):
            normalized = "https://static.cninfo.com.cn/" + locator
        else:
            raise ValueError("unsupported exact index locator")
        validate_url(normalized, "original")
        return normalized

    @staticmethod
    def _safe_headers(headers: dict[str, str]) -> tuple[tuple[str, str], ...]:
        allow = {"content-type", "content-length", "location", "retry-after", "date", "etag", "last-modified"}
        return tuple(sorted((key.lower(), value) for key, value in headers.items() if key.lower() in allow))

    @staticmethod
    def _response_failure(purpose: Purpose, response: SourceResponse) -> str | None:
        if response.status in {301, 302, 303, 307, 308}:
            return None
        if response.status != 200:
            return f"http_status_{response.status}"
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if purpose in {"directory", "query"} and mime not in {"application/json", "text/json"}:
            return "unexpected_json_mime"
        if purpose == "original" and mime != "application/pdf":
            return "unexpected_pdf_mime"
        if purpose == "terms" and mime not in {"text/html", "application/pdf", "text/plain"}:
            return "unexpected_terms_mime"
        return None

    @staticmethod
    def _validate_fixed_query_body(body: bytes | None) -> None:
        if body is None:
            raise DisclosureError("probe_query_body_required")
        try:
            pairs = parse_qsl(body.decode("ascii"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeError, ValueError) as exc:
            raise DisclosureError("probe_query_body_invalid") from exc
        params: dict[str, str] = {}
        for key, value in pairs:
            if key in params:
                raise DisclosureError("probe_query_duplicate_parameter")
            params[key] = value
        expected_keys = {
            "pageNum", "pageSize", "column", "tabName", "stock", "seDate", "sortName",
            "sortType", "plate", "searchkey", "secid", "category", "trade", "isHLtitle",
        }
        if set(params) != expected_keys:
            raise DisclosureError("probe_query_parameter_set_mismatch")
        if (
            params["pageNum"] not in {"1", "2"}
            or params["pageSize"] != "20"
            or params["column"] != "szse"
            or params["tabName"] != "fulltext"
            or params["seDate"] != "2026-04-27~2026-05-03"
            or not re.fullmatch(r"\d{6},[A-Za-z0-9_-]{1,64}", params["stock"])
            or not params["sortName"]
            or params["sortType"] != "desc"
            or params["plate"] != ""
            or params["searchkey"] != ""
            or params["secid"] != ""
            or params["category"] != ""
            or params["trade"] != ""
            or params["isHLtitle"] != "false"
        ):
            raise DisclosureError("probe_query_scope_mismatch")

    @staticmethod
    def _redirect_target(
        response: SourceResponse,
        *,
        purpose: Purpose,
        depth: int,
        body_present: bool,
        exact_urls: tuple[str, ...],
    ) -> str:
        if response.status not in {301, 302, 303, 307, 308}:
            raise DisclosureError("probe_expected_redirect_missing")
        if depth > 2 or (body_present and response.status not in {307, 308}):
            raise DisclosureError("probe_redirect_limit_or_method_change")
        from urllib.parse import urljoin

        location = response.headers.get("location")
        if not location:
            raise DisclosureError("probe_redirect_location_missing")
        target = urljoin(response.url, location)
        validate_url(target, purpose, exact_urls=exact_urls)
        return target


class _TestDisclosureProbeExecutor(DisclosureProbeExecutor):
    """Explicit fixture-only executor returned by for_tests."""

