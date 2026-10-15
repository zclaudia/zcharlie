"""Rejected replay inputs and parser drift cannot leave new financial rows."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.archive import (
    ArchiveCapture,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
    record_acquisition,
)
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.contracts import ParseResult, UpstreamRequest
from investment_agent.materials.replay import ReplayError, replay_acquisition
from investment_agent.storage.models.research_material import ResearchDataset
from tests.integration.test_material_collection_job import _draft

SOURCE = "eastmoney.hk.native.v1"


async def _capture(session, data_dir):
    identity = next(
        item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if item.company_key == "clp-holdings"
    )
    setup = await initialize_pilot(session, (identity,))
    archived = archive_capture(data_dir, ArchiveCapture(
        identity=identity_snapshot(identity), subject_id=setup.company_ids[identity.company_key],
        source=SOURCE, adapter_version="test-parser-1", periods=(),
        expected_requests=("income",),
        responses=(ResponseEvidence(
            name="income", content=b"{}", source_url="https://example.invalid/income",
            media_type="application/json", http_status=200, fetched_at=datetime.now(UTC),
        ),),
        problems=("original attempt produced no dataset",),
    ))
    row = await record_acquisition(session, archived)
    return identity, row


class DriftedAdapter:
    source = SOURCE
    adapter_version = "test-parser-1"

    def requests(self, identity, *, periods):
        return (UpstreamRequest(name="income", url="https://example.invalid/income"),)

    def parse(self, identity, *, raw, periods, subject_id=None):
        return ParseResult(
            drafts=(replace(
                _draft(source=self.source, subject_id=subject_id),
                adapter_version=self.adapter_version,
            ),), problems=(),
        )


async def test_parser_drift_rolls_back_even_if_caller_catches_error(db_session, tmp_path):
    identity, row = await _capture(db_session, tmp_path)
    with pytest.raises(ReplayError, match="trusted links"):
        await replay_acquisition(
            db_session, acquisition_id=row.id, data_dir=tmp_path,
            identities=(identity,), adapters={SOURCE: DriftedAdapter()},
        )
    # The caller may keep using or commit its outer transaction after the refusal.
    assert await db_session.scalar(select(func.count()).select_from(ResearchDataset)) == 0
    assert row.dataset_links == []


async def test_identity_version_and_tampered_manifest_are_rejected(db_session, tmp_path: Path):
    identity, row = await _capture(db_session, tmp_path)
    changed = replace(identity, company_name="Different company")
    with pytest.raises(ReplayError, match="identity"):
        await replay_acquisition(
            db_session, acquisition_id=row.id, data_dir=tmp_path,
            identities=(changed,), adapters={SOURCE: DriftedAdapter()},
        )
    adapter = DriftedAdapter()
    adapter.adapter_version = "test-parser-2"
    with pytest.raises(ReplayError, match="version mismatch"):
        await replay_acquisition(
            db_session, acquisition_id=row.id, data_dir=tmp_path,
            identities=(identity,), adapters={SOURCE: adapter},
        )
    manifest = tmp_path / row.archive_ref
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ReplayError, match="SHA-256"):
        await replay_acquisition(
            db_session, acquisition_id=row.id, data_dir=tmp_path,
            identities=(identity,), adapters={SOURCE: DriftedAdapter()},
        )
    assert await db_session.scalar(select(func.count()).select_from(ResearchDataset)) == 0
