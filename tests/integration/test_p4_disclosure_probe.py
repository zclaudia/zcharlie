from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    DisclosureError,
    OfficialHTTPS,
    SourceResponse,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models import Base
from investment_agent.storage.models.disclosure import DisclosureItem, DisclosureOriginal
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_campaign import (
    OWNER,
    amend_disclosure_campaign,
    create_disclosure_campaign,
    read_campaign_hop_quarantine,
    read_disclosure_campaign,
)
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    CampaignAction,
    DisclosureCampaignV2,
)
from investment_agent.supplemental_rules.disclosure_probe import (
    AtomicDirectoryQuarantineSink,
    CampaignProbeLedger,
    DisclosureProbeApprovalV1,
    DisclosureProbeExecutor,
    ProbeApprovalRegistry,
    ProbeOperationScope,
    ProbeRequest,
    current_probe_build,
    probe_approval_hash,
)


class MemoryCampaignLedger:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    async def reserve(self, operation_key, purpose, url, request_bytes, max_response_bytes, **scope):
        self.rows.append(("reserved", operation_key, purpose))
        return {"permit_id": operation_key, "storage_bytes": max_response_bytes + 1024 * 1024}

    async def mark_sent(self, permit_ref):
        self.rows.append(("sent", permit_ref["permit_id"], ""))

    async def settle(self, permit_ref, *, outcome, response_bytes, response_hash):
        self.rows.append((outcome, permit_ref["permit_id"], response_hash or ""))

    async def cancel(self, permit_ref, *, reason):
        self.rows.append(("canceled", permit_ref["permit_id"], reason))

    async def record_quarantine(self, permit_ref, *, outcome, reason):
        self.rows.append((f"quarantine:{outcome}", permit_ref["permit_id"], reason))


class MemoryQuarantine:
    def __init__(self) -> None:
        self.captures = []
        self.body_reads = 0

    def append_raw(self, capture):
        self.captures.append(capture)

    def read_audit(self, approval_hash, operation_key):
        capture = next((
            item for item in self.captures
            if item.approval_hash == approval_hash and item.operation_key == operation_key
        ), None)
        if capture is None:
            return None
        return {
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
            "failure_reason": capture.failure_reason,
            "response_body_sha256": capture.response_sha256,
            "response_bytes": capture.response_bytes,
            "outcome": capture.outcome,
        }

    def read_capture(self, approval_hash, operation_key):
        self.body_reads += 1
        audit = self.read_audit(approval_hash, operation_key)
        capture = next((item for item in self.captures if item.operation_key == operation_key), None)
        if audit is None or capture is None:
            return None
        return audit, capture.response_body


class ControlledHTTP:
    def __init__(self, response: SourceResponse | list[SourceResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[tuple[str, str]] = []

    async def request_hop(self, url, purpose, *, body, max_bytes):
        self.calls.append((url, purpose))
        return self.responses.pop(0)


class RevokingHTTP(ControlledHTTP):
    def __init__(self, response: SourceResponse, registry, approval_hash: str, now: datetime) -> None:
        super().__init__(response)
        self.registry = registry
        self.approval_hash = approval_hash
        self.now = now

    async def request_hop(self, url, purpose, *, body, max_bytes):
        response = await super().request_hop(url, purpose, body=body, max_bytes=max_bytes)
        self.registry.revoke(self.approval_hash, at=self.now, reference="synthetic revoke")
        return response


class RevokingReserveLedger(MemoryCampaignLedger):
    def __init__(self, registry, approval_hash: str, now: datetime) -> None:
        super().__init__()
        self.registry = registry
        self.approval_hash = approval_hash
        self.now = now

    async def reserve(self, operation_key, purpose, url, request_bytes, max_response_bytes, **scope):
        permit = await super().reserve(operation_key, purpose, url, request_bytes, max_response_bytes, **scope)
        self.registry.revoke(self.approval_hash, at=self.now, reference="synthetic pre-send revoke")
        return permit


class FailingQuarantine:
    def append_raw(self, capture):
        raise OSError("synthetic disk failure")

    def read_audit(self, approval_hash, operation_key):
        return None

    def read_capture(self, approval_hash, operation_key):
        return None


@pytest_asyncio.fixture
async def probe_file_database(tmp_path):
    database = (tmp_path / "probe-ledger.sqlite").resolve()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session, factory, str(database)
    finally:
        await engine.dispose()


def _approval(
    now: datetime,
    *,
    two_hops: bool = False,
    campaign_id=None,
    action_id: str = "synthetic-terms-1",
) -> DisclosureProbeApprovalV1:
    scopes = [
        ProbeOperationScope(
            campaign_action_id=action_id,
            purpose="directory",
            url=DIRECTORY,
            method="GET",
            max_response_bytes=8 * 1024 * 1024,
            max_storage_bytes=9 * 1024 * 1024,
        )
    ]
    if two_hops:
        scopes.extend(
            [
                ProbeOperationScope(
                    campaign_action_id="synthetic-terms-2",
                    purpose="terms",
                    url="https://www.cninfo.com.cn/disclosure/terms-a.html",
                    method="GET",
                    max_response_bytes=1024 * 1024,
                    max_storage_bytes=2 * 1024 * 1024,
                    previous_operation_index=0,
                ),
                ProbeOperationScope(
                    campaign_action_id="synthetic-terms-3",
                    purpose="terms",
                    url="https://www.cninfo.com.cn/disclosure/terms-b.html",
                    method="GET",
                    max_response_bytes=1024 * 1024,
                    max_storage_bytes=2 * 1024 * 1024,
                    redirect_depth=1,
                    previous_operation_index=1,
                ),
            ]
        )
    data_dir = f"/tmp/synthetic-probe-data-{uuid4()}"
    raw = {
        "schema_version": 1,
        "campaign_id": campaign_id or uuid4(),
        "sample_env_id": "synthetic-isolated-db",
        "approval_ref": "synthetic-approval",
        "executor_build": current_probe_build(),
        "data_dir": data_dir,
        "quarantine_dir": f"{data_dir}/raw",
        "evidence_hashes": ("b" * 64,),
        "executor_name": "synthetic operator",
        "reviewer_name": "synthetic separate reviewer",
        "valid_from": now - timedelta(minutes=1),
        "expires_at": now + timedelta(hours=1),
        "operations": tuple(scopes),
        "approval_hash": "0" * 64,
    }
    raw["approval_hash"] = probe_approval_hash(raw)
    return DisclosureProbeApprovalV1.model_validate(raw)


@pytest.mark.asyncio
async def test_probe_is_raw_only_and_campaign_accounts_every_single_hop() -> None:
    now = datetime.now(UTC)
    approval = _approval(now)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    ledger = MemoryCampaignLedger()
    quarantine = MemoryQuarantine()
    http = ControlledHTTP(
        SourceResponse(429, {"retry-after": "60"}, b"throttled", DIRECTORY)
    )
    executor = DisclosureProbeExecutor.for_tests(
        ledger=ledger, quarantine=quarantine, http=http, registry=registry
    )

    capture = await executor.capture(
        ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
        operation_index=0,
        approval_hash=approval.approval_hash,
        now=now,
    )

    assert capture.status == 429
    assert capture.response_sha256 == hashlib.sha256(b"throttled").hexdigest()
    assert capture.permit_id
    assert len(quarantine.captures) == 1
    assert [stage for stage, _, _ in ledger.rows] == ["reserved", "sent", "terminal_error", "quarantine:stored"]
    assert len(http.calls) == 1
    with pytest.raises(DisclosureError, match="already_captured"):
        await executor.capture(
            ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
            operation_index=0, approval_hash=approval.approval_hash, now=now,
        )
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_raw_capture_sink_writes_quarantine_blob_and_audit_pair(tmp_path) -> None:
    now = datetime.now(UTC)
    approval = _approval(now)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    quarantine_dir = tmp_path / "raw-quarantine"
    quarantine_dir.mkdir(mode=0o700)
    sink = AtomicDirectoryQuarantineSink(quarantine_dir)
    executor = DisclosureProbeExecutor.for_tests(
        ledger=MemoryCampaignLedger(),
        quarantine=sink,
        http=ControlledHTTP(SourceResponse(200, {"content-type": "application/json"}, b"raw directory", DIRECTORY)),
        registry=registry,
    )

    capture = await executor.capture(
        ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
        operation_index=0, approval_hash=approval.approval_hash, now=now,
    )
    stem = f"{approval.approval_hash}-{capture.operation_key}"
    assert (quarantine_dir / f"{stem}.raw").read_bytes() == b"raw directory"
    audit = json.loads((quarantine_dir / f"{stem}.json").read_text())
    assert audit["response_body_sha256"] == hashlib.sha256(b"raw directory").hexdigest()
    assert audit["permit_id"] == capture.permit_id


@pytest.mark.asyncio
async def test_probe_never_follows_unapproved_or_unobserved_redirects() -> None:
    now = datetime.now(UTC)
    approval = _approval(now, two_hops=True)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    ledger = MemoryCampaignLedger()
    quarantine = MemoryQuarantine()
    http = ControlledHTTP([
        SourceResponse(200, {"content-type": "application/json"}, b"done", DIRECTORY),
        SourceResponse(200, {"content-type": "text/html"}, b"done", approval.operations[1].url),
    ])
    executor = DisclosureProbeExecutor.for_tests(
        ledger=ledger, quarantine=quarantine, http=http, registry=registry
    )
    await executor.capture(
        ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
        operation_index=0, approval_hash=approval.approval_hash, now=now,
    )
    await executor.capture(
        ProbeRequest("terms", approval.operations[1].url, max_bytes=1024 * 1024),
        operation_index=1, approval_hash=approval.approval_hash, now=now,
    )
    with pytest.raises(DisclosureError, match="expected_redirect_missing"):
        await executor.capture(
            ProbeRequest("terms", approval.operations[2].url, max_bytes=1024 * 1024),
            operation_index=2, approval_hash=approval.approval_hash, now=now,
        )
    assert len(http.calls) == 2
    assert len(quarantine.captures) == 2


@pytest.mark.asyncio
async def test_campaign_probe_ledger_persists_s0_usage_in_sqlite(probe_file_database) -> None:
    db_session, factory, database_path = probe_file_database
    now = datetime.now(UTC)
    action = CampaignAction(
        action_id="synthetic-directory", kind="raw_probe", status="approved",
        url_allowlist=(DIRECTORY,), method="GET", http_cap=1,
        response_byte_cap=8 * 1024 * 1024, storage_byte_cap=9 * 1024 * 1024,
    )
    campaign = DisclosureCampaignV2(
        campaign_id=uuid4(), revision=1, approval_ref="synthetic-campaign-approval",
        sample_env_id="synthetic-isolated-db",
        database_path=database_path,
        valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(days=1),
        http_cap=32, response_bytes_cap=192 * 1024 * 1024, storage_bytes_cap=512 * 1024 * 1024,
        s0_http_cap=8, s0_response_bytes_cap=40 * 1024 * 1024,
        s0_storage_bytes_cap=128 * 1024 * 1024, sample_http_cap=24,
        sample_response_bytes_cap=152 * 1024 * 1024, actions=(action,),
    )
    campaign = await create_disclosure_campaign(db_session, campaign, now=now)
    await db_session.commit()
    data_dir = f"/tmp/synthetic-probe-data-{uuid4()}"
    approval_raw = {
        "schema_version": 1, "campaign_id": campaign.campaign_id,
        "sample_env_id": campaign.sample_env_id, "approval_ref": "synthetic-independent-approval",
        "data_dir": data_dir,
        "quarantine_dir": f"{data_dir}/raw",
        "executor_build": current_probe_build(),
        "evidence_hashes": ("c" * 64,), "executor_name": "synthetic operator",
        "reviewer_name": "synthetic independent reviewer",
        "valid_from": now - timedelta(minutes=1), "expires_at": now + timedelta(hours=1),
        "operations": (ProbeOperationScope(
            campaign_action_id=action.action_id, purpose="directory", url=DIRECTORY,
            method="GET", max_response_bytes=8 * 1024 * 1024,
            max_storage_bytes=9 * 1024 * 1024,
        ),),
        "approval_hash": "0" * 64,
    }
    approval_raw["approval_hash"] = probe_approval_hash(approval_raw)
    approval = DisclosureProbeApprovalV1.model_validate(approval_raw)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    quarantine = MemoryQuarantine()
    executor = DisclosureProbeExecutor.for_tests(
        ledger=CampaignProbeLedger(session_factory=factory), quarantine=quarantine,
        http=ControlledHTTP(SourceResponse(200, {"content-type": "application/json"}, b"synthetic directory", DIRECTORY)),
        registry=registry,
    )

    capture = await executor.capture(
        ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
        operation_index=0, approval_hash=approval.approval_hash, now=now,
    )
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.http_consumed == 1 and saved.s0_http_consumed == 1
    assert saved.response_bytes_consumed == len(b"synthetic directory")
    assert saved.storage_bytes_consumed == len(b"synthetic directory") + 1024 * 1024
    quarantine_receipt = await SupplementalUpdatesRepository(db_session).get_change_by_key(
        OWNER, f"p4:campaign-hop:{capture.permit_id}:quarantine:stored",
    )
    assert quarantine_receipt is not None
    assert quarantine_receipt.payload["status"] == "stored"
    progress = await executor.read_progress(approval.approval_hash)
    assert progress["operations"][0]["state"] == "stored"
    assert progress["operations"][0]["sent"] is True
    await db_session.commit()
    paused = await amend_disclosure_campaign(
        db_session,
        campaign_id=campaign.campaign_id,
        expected_revision=saved.revision,
        patch={"status": "paused"},
        approval_ref="synthetic-pause-approval",
        now=now + timedelta(minutes=1),
    )
    await db_session.commit()
    assert paused.status == "paused"
    with pytest.raises(DisclosureError, match="campaign_authority_changed"):
        await executor.capture(
            ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
            operation_index=0, approval_hash=approval.approval_hash,
            now=now + timedelta(minutes=2),
        )
    assert len(quarantine.captures) == 1
    assert quarantine.body_reads == 0


@pytest.mark.asyncio
async def test_sink_failure_leaves_campaign_raw_failed_receipt(probe_file_database) -> None:
    db_session, factory, database_path = probe_file_database
    now = datetime.now(UTC)
    action = CampaignAction(
        action_id="synthetic-sink-failure", kind="raw_probe", status="approved",
        url_allowlist=(DIRECTORY,), method="GET", http_cap=1,
        response_byte_cap=8 * 1024 * 1024, storage_byte_cap=9 * 1024 * 1024,
    )
    campaign = DisclosureCampaignV2(
        campaign_id=uuid4(), revision=1, approval_ref="synthetic-campaign-approval",
        sample_env_id="synthetic-isolated-db",
        database_path=database_path,
        valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(days=1),
        http_cap=32, response_bytes_cap=192 * 1024 * 1024, storage_bytes_cap=512 * 1024 * 1024,
        s0_http_cap=8, s0_response_bytes_cap=40 * 1024 * 1024,
        s0_storage_bytes_cap=128 * 1024 * 1024, sample_http_cap=24,
        sample_response_bytes_cap=152 * 1024 * 1024, actions=(action,),
    )
    campaign = await create_disclosure_campaign(db_session, campaign, now=now)
    await db_session.commit()
    data_dir = f"/tmp/synthetic-probe-data-{uuid4()}"
    raw = {
        "schema_version": 1, "campaign_id": campaign.campaign_id,
        "sample_env_id": campaign.sample_env_id, "approval_ref": "synthetic-independent-approval",
        "data_dir": data_dir, "quarantine_dir": f"{data_dir}/raw",
        "executor_build": current_probe_build(), "evidence_hashes": ("d" * 64,),
        "executor_name": "synthetic operator", "reviewer_name": "synthetic reviewer",
        "valid_from": now - timedelta(minutes=1), "expires_at": now + timedelta(hours=1),
        "operations": (ProbeOperationScope(
            campaign_action_id=action.action_id, purpose="directory", url=DIRECTORY,
            method="GET", max_response_bytes=8 * 1024 * 1024,
            max_storage_bytes=9 * 1024 * 1024,
        ),),
        "approval_hash": "0" * 64,
    }
    raw["approval_hash"] = probe_approval_hash(raw)
    approval = DisclosureProbeApprovalV1.model_validate(raw)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    executor = DisclosureProbeExecutor.for_tests(
        ledger=CampaignProbeLedger(session_factory=factory), quarantine=FailingQuarantine(),
        http=ControlledHTTP(SourceResponse(
            200, {"content-type": "application/json"}, b"synthetic response", DIRECTORY,
        )),
        registry=registry,
    )
    with pytest.raises(DisclosureError, match="raw_pending_after_sink_failure"):
        await executor.capture(
            ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
            operation_index=0, approval_hash=approval.approval_hash, now=now,
        )
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.s0_http_consumed == 1
    operation_key = hashlib.sha256(
        f"{approval.approval_hash}:0:directory:{DIRECTORY}:None".encode()
    ).hexdigest()
    permit_id = uuid5(
        NAMESPACE_URL,
        f"p4-campaign:{campaign.campaign_id}:s0:{action.action_id}:{operation_key}",
    )
    receipt = await SupplementalUpdatesRepository(db_session).get_change_by_key(
        OWNER, f"p4:campaign-hop:{permit_id}:quarantine:failed",
    )
    assert receipt is not None and receipt.payload["status"] == "failed"
    quarantine_state = await read_campaign_hop_quarantine(db_session, permit_id=permit_id)
    assert quarantine_state["state"] == "failed"


@pytest.mark.parametrize("was_sent", (False, True), ids=("reserved-cancel", "sent-unknown"))
@pytest.mark.asyncio
async def test_file_ledger_recovers_exact_raw_orphan_without_network(
    probe_file_database, was_sent: bool,
) -> None:
    db_session, factory, database_path = probe_file_database
    now = datetime.now(UTC)
    sample_env_id = "synthetic-recovery-env"
    action_id = f"synthetic-recovery-{uuid4()}"
    action = CampaignAction(
        action_id=action_id, kind="raw_probe", status="approved",
        url_allowlist=(DIRECTORY,), method="GET", http_cap=1,
        response_byte_cap=8 * 1024 * 1024, storage_byte_cap=9 * 1024 * 1024,
    )
    campaign = DisclosureCampaignV2(
        campaign_id=uuid4(), revision=1, approval_ref="synthetic-campaign-approval",
        sample_env_id=sample_env_id, database_path=database_path,
        valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(days=1),
        http_cap=32, response_bytes_cap=192 * 1024 * 1024, storage_bytes_cap=512 * 1024 * 1024,
        s0_http_cap=8, s0_response_bytes_cap=40 * 1024 * 1024,
        s0_storage_bytes_cap=128 * 1024 * 1024, sample_http_cap=24,
        sample_response_bytes_cap=152 * 1024 * 1024, actions=(action,),
    )
    campaign = await create_disclosure_campaign(db_session, campaign, now=now)
    await db_session.commit()

    approval = _approval(now, campaign_id=campaign.campaign_id, action_id=action_id)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    recovery_at = now + timedelta(minutes=2)
    registry.revoke(approval.approval_hash, at=now + timedelta(seconds=1), reference="synthetic revoke")

    first_ledger = CampaignProbeLedger(session_factory=factory)
    operation_key = hashlib.sha256(
        f"{approval.approval_hash}:0:directory:{DIRECTORY}:None".encode()
    ).hexdigest()
    permit = await first_ledger.reserve(
        operation_key, "directory", DIRECTORY, 0, 8 * 1024 * 1024,
        campaign_id=campaign.campaign_id, campaign_action_id=action_id,
        sample_env_id=sample_env_id, max_storage_bytes=9 * 1024 * 1024,
        body_hash=None,
    )
    if was_sent:
        await first_ledger.mark_sent(permit)
    await db_session.rollback()
    paused = await amend_disclosure_campaign(
        db_session, campaign_id=campaign.campaign_id,
        expected_revision=campaign.revision + 1 + int(was_sent),
        patch={"status": "paused"}, approval_ref="synthetic-pause-approval",
        now=now + timedelta(seconds=30),
    )
    assert paused.status == "paused"
    await db_session.commit()

    # A fresh durable adapter resolves accounting despite the revoked capture
    # approval and paused campaign; neither condition grants another HTTP hop.
    recovery_ledger = CampaignProbeLedger(session_factory=factory)
    with pytest.raises(InvestorError):
        await recovery_ledger.recover_hop(
            permit_id=UUID(permit["permit_id"]), campaign_id=campaign.campaign_id,
            sample_env_id="wrong-environment", operation_id=uuid4(),
            recovery_approval_ref="synthetic-recovery-signoff", now=recovery_at,
        )
    with pytest.raises(InvestorError):
        await recovery_ledger.recover_hop(
            permit_id=uuid4(), campaign_id=campaign.campaign_id,
            sample_env_id=sample_env_id, operation_id=uuid4(),
            recovery_approval_ref="synthetic-recovery-signoff", now=recovery_at,
        )
    with pytest.raises(InvestorError):
        await recovery_ledger.recover_hop(
            permit_id=UUID(permit["permit_id"]), campaign_id=uuid4(),
            sample_env_id=sample_env_id, operation_id=uuid4(),
            recovery_approval_ref="synthetic-recovery-signoff", now=recovery_at,
        )
    operation_id = uuid4()
    with pytest.raises(InvestorError, match="recovery approval"):
        await recovery_ledger.recover_hop(
            permit_id=UUID(permit["permit_id"]), campaign_id=campaign.campaign_id,
            sample_env_id=sample_env_id, operation_id=operation_id,
            recovery_approval_ref=" ", now=recovery_at,
        )
    decision = await recovery_ledger.recover_hop(
        permit_id=UUID(permit["permit_id"]), campaign_id=campaign.campaign_id,
        sample_env_id=sample_env_id, operation_id=operation_id,
        recovery_approval_ref="synthetic-recovery-signoff", now=recovery_at,
    )
    expected_state = "unknown" if was_sent else "canceled"
    assert decision["outcome"] == expected_state
    assert decision["allow_network"] is False
    assert decision["approval_ref"] == "synthetic-recovery-signoff"
    assert await recovery_ledger.recover_hop(
        permit_id=UUID(permit["permit_id"]), campaign_id=campaign.campaign_id,
        sample_env_id=sample_env_id, operation_id=operation_id,
        recovery_approval_ref="synthetic-recovery-signoff", now=recovery_at + timedelta(minutes=1),
    ) == decision
    with pytest.raises(InvestorError, match="different scope"):
        await recovery_ledger.recover_hop(
            permit_id=UUID(permit["permit_id"]), campaign_id=campaign.campaign_id,
            sample_env_id=sample_env_id, operation_id=operation_id,
            recovery_approval_ref="different-recovery-signoff", now=recovery_at,
        )
    with pytest.raises(InvestorError):
        await recovery_ledger.mark_sent(permit)

    from investment_agent.supplemental_rules import disclosure_authorization as auth

    day = auth._day(now)
    _, owner = await auth._quota_state(db_session, f"owner-day:{day}", day=day)
    _, storage = await auth._quota_state(db_session, auth.STORAGE_SCOPE)
    _, cooldown = await auth._quota_state(db_session, auth.COOLDOWN_SCOPE)
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert owner["request_reserved"] == 0
    assert owner["request_consumed"] == int(was_sent)
    assert owner["bytes_reserved"] == 0
    assert owner["bytes_consumed"] == (8 * 1024 * 1024 if was_sent else 0)
    assert storage["storage_reserved"] == 0
    assert storage["storage_consumed"] == (9 * 1024 * 1024 if was_sent else 0)
    assert cooldown["active_hop"] is None
    assert saved.active_poll is None
    assert saved.http_reserved == 0
    assert saved.http_consumed == int(was_sent)
    assert saved.response_bytes_consumed == (8 * 1024 * 1024 if was_sent else 0)
    assert saved.storage_bytes_consumed == (9 * 1024 * 1024 if was_sent else 0)
    recovery_receipt = await SupplementalUpdatesRepository(db_session).get_change_by_key(
        OWNER, f"p4:s0-recovery:{operation_id}",
    )
    assert recovery_receipt is not None
    assert await db_session.scalar(select(func.count()).select_from(DisclosureItem)) == 0
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0


@pytest.mark.asyncio
async def test_revocation_after_response_settles_hash_only_without_raw_body() -> None:
    now = datetime.now(UTC)
    approval = _approval(now)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    ledger = MemoryCampaignLedger()
    quarantine = MemoryQuarantine()
    http = RevokingHTTP(
        SourceResponse(200, {"content-type": "application/json"}, b"private response", DIRECTORY),
        registry, approval.approval_hash, now,
    )
    executor = DisclosureProbeExecutor.for_tests(
        ledger=ledger, quarantine=quarantine, http=http, registry=registry,
    )
    with pytest.raises(DisclosureError, match="authority_changed_after_response"):
        await executor.capture(
            ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
            operation_index=0, approval_hash=approval.approval_hash, now=now,
        )
    assert quarantine.captures == []
    assert [stage for stage, _, _ in ledger.rows][-2:] == ["withheld", "quarantine:withheld"]


@pytest.mark.asyncio
async def test_indexed_operation_cannot_skip_or_relabel_original_index() -> None:
    now = datetime.now(UTC)
    approval = _approval(now, two_hops=True)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    http = ControlledHTTP(SourceResponse(200, {"content-type": "application/json"}, b"{}", DIRECTORY))
    executor = DisclosureProbeExecutor.for_tests(
        ledger=MemoryCampaignLedger(), quarantine=MemoryQuarantine(), http=http, registry=registry,
    )
    with pytest.raises(DisclosureError, match="previous_operation_unavailable"):
        await executor.capture(
            ProbeRequest("terms", approval.operations[1].url, max_bytes=1024 * 1024),
            operation_index=1, approval_hash=approval.approval_hash, now=now,
        )
    assert http.calls == []


@pytest.mark.asyncio
async def test_revocation_after_reserve_cancels_before_http_send() -> None:
    now = datetime.now(UTC)
    approval = _approval(now)
    registry = ProbeApprovalRegistry.for_tests()
    registry.install_approved(approval)
    ledger = RevokingReserveLedger(registry, approval.approval_hash, now)
    http = ControlledHTTP(SourceResponse(200, {"content-type": "application/json"}, b"should not send", DIRECTORY))
    executor = DisclosureProbeExecutor.for_tests(
        ledger=ledger, quarantine=MemoryQuarantine(), http=http, registry=registry,
    )
    with pytest.raises(DisclosureError, match="authority_changed_before_send"):
        await executor.capture(
            ProbeRequest("directory", DIRECTORY, max_bytes=8 * 1024 * 1024),
            operation_index=0, approval_hash=approval.approval_hash, now=now,
        )
    assert http.calls == []
    assert [stage for stage, _, _ in ledger.rows] == ["reserved", "canceled"]


def test_live_probe_constructor_rejects_fake_ledger_and_sink() -> None:
    with pytest.raises(DisclosureError, match="server_owned"):
        DisclosureProbeExecutor(
            ledger=MemoryCampaignLedger(),  # type: ignore[arg-type]
            quarantine=MemoryQuarantine(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file:probe?mode=memory&cache=shared",
    ),
)
@pytest.mark.asyncio
async def test_live_probe_rejects_volatile_ledger_before_any_http(tmp_path, database_url) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    quarantine_path = tmp_path / "raw"
    quarantine_path.mkdir(mode=0o700)
    quarantine_path.chmod(0o700)

    class CountingOfficialHTTPS(OfficialHTTPS):
        def __init__(self) -> None:
            self.calls = 0

        async def request_one_hop(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("volatile ledger must be rejected before HTTP")

    http = CountingOfficialHTTPS()
    with pytest.raises(DisclosureError, match="ledger_requires_file_sqlite"):
        DisclosureProbeExecutor(
            ledger=CampaignProbeLedger(session_factory=factory),
            quarantine=AtomicDirectoryQuarantineSink(quarantine_path),
            http=http,
            data_dir=tmp_path,
        )
    assert http.calls == 0
    await engine.dispose()
