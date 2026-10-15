"""Source collection writes one immutable acquisition for every actual attempt."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from investment_agent.domain.security import ControlledIdentity
from investment_agent.materials import handlers
from investment_agent.materials.archive import verify_capture
from investment_agent.materials.contracts import (
    FactPeriod,
    ParseResult,
    RawPayload,
    UpstreamRequest,
)
from investment_agent.materials.handlers import HttpxMaterialFetcher, MaterialCollectionHandler
from investment_agent.materials.jobs import MaterialCollectionPayload
from investment_agent.providers.errors import ProviderTransientError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from tests.integration.test_material_collection_job import (
    BATCH,
    HK_SOURCE,
    _claim,
    _Ctx,
    _dataset_count,
    _draft,
    _listing_id,
    _open_the_gate,
    _run_row,
    _seed,
    _settings,
)


@dataclass
class ArchiveAdapter:
    source: str = HK_SOURCE
    adapter_version: str = "fake-1"
    names: tuple[str, ...] = ("income.json", "balance.json")
    emit_draft: bool = True
    problems: tuple[str, ...] = ()
    request_periods: list[tuple[FactPeriod, ...]] = field(default_factory=list)

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        self.request_periods.append(tuple(periods))
        return tuple(
            UpstreamRequest(name=name, url=f"https://example.invalid/{name}")
            for name in self.names
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        drafts = (_draft(source=self.source, subject_id=subject_id),) if self.emit_draft else ()
        return ParseResult(drafts=drafts, problems=self.problems)


@dataclass
class ScriptedFetcher:
    responses: dict[str, RawPayload]
    fail_on_call: int | None = None
    error: Exception | None = None
    calls: list[UpstreamRequest] = field(default_factory=list)

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload:
        self.calls.append(request)
        if self.fail_on_call == len(self.calls) and self.error is not None:
            raise self.error
        return self.responses[request.name]


def _raw(name: str, body: bytes, *, fetched_at: datetime | None = None, status: int = 200) -> RawPayload:
    return RawPayload(
        name=name,
        content=body,
        media_type="application/json",
        fetched_at=fetched_at or datetime(2026, 10, 12, tzinfo=UTC),
        source_url=f"https://example.invalid/{name}?api_key=private",
        http_status=status,
    )


async def _acquisitions():
    async with session_scope() as session:
        return list(
            (await session.execute(select(ResearchMaterialAcquisition))).scalars()
        )


async def test_default_offline_attempt_creates_no_fake_acquisition(
    db_session, tmp_path: Path
) -> None:
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = ScriptedFetcher(responses={})
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: ArchiveAdapter()}
    )
    job, _ = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH)
    )

    assert handlers.ONLINE_REFRESH_ENABLED is False
    assert await handler.execute(job, _Ctx()) == "partial"
    assert fetcher.calls == []
    assert await _acquisitions() == []


async def test_multi_response_capture_records_periods_requests_and_dataset_link(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    acquired = datetime(2026, 10, 12, 4, tzinfo=UTC)
    fetcher = ScriptedFetcher(
        responses={
            "income.json": _raw("income.json", b'{"income":1}', fetched_at=acquired),
            "balance.json": _raw("balance.json", b'{"balance":2}', fetched_at=acquired),
        }
    )
    adapter = ArchiveAdapter()
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id,
            sources=(HK_SOURCE,),
            periods=("2024-12-31", "2023-12-31"),
            refresh_batch=BATCH,
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    (acquisition,) = await _acquisitions()
    capture = verify_capture(tmp_path, acquisition.archive_ref, acquisition.manifest_sha256)
    assert acquisition.subject_id == companies["hsbc-holdings"]
    assert acquisition.source == HK_SOURCE
    assert acquisition.adapter_version == adapter.adapter_version
    assert acquisition.expected_requests == ["income.json", "balance.json"]
    assert acquisition.periods == [
        {"start": None, "end": "2023-12-31", "basis": "annual", "label": None, "days": None},
        {"start": None, "end": "2024-12-31", "basis": "annual", "label": None, "days": None},
    ]
    assert [response.content for response in capture.responses] == [b'{"income":1}', b'{"balance":2}']
    assert all("private" not in response.source_url for response in capture.responses)
    assert len(acquisition.dataset_links) == 1
    assert acquisition.dataset_links[0]["revision"] == 1
    run = await _run_row(run_id)
    assert run.metadata_json["acquisitions"][0]["manifest_sha256"] == acquisition.manifest_sha256


async def test_no_drafts_and_partial_transient_attempts_still_archive_responses(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    adapter = ArchiveAdapter(emit_draft=False, names=("table-a.json", "table-b.json"))
    fetcher = ScriptedFetcher(
        responses={
            "table-a.json": _raw("table-a.json", b'{"ok":1}'),
            "table-b.json": _raw("table-b.json", b'{"ok":2}'),
        }
    )
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, _ = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH)
    )
    assert await handler.execute(job, _Ctx()) == "partial"
    first, = await _acquisitions()
    assert first.dataset_links == []
    assert any("no_dataset" in problem for problem in first.problems)
    assert [item.name for item in verify_capture(tmp_path, first.archive_ref).responses] == [
        "table-a.json", "table-b.json"
    ]

    second_job, _ = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch="2026-10-13")
    )
    partial_fetcher = ScriptedFetcher(
        responses={"table-a.json": _raw("table-a.json", b'{"ok":3}')},
        fail_on_call=2,
        error=ProviderTransientError("temporary", provider=HK_SOURCE),
    )
    partial_handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=partial_fetcher,
        adapters={HK_SOURCE: ArchiveAdapter(names=("table-a.json", "table-b.json"))},
    )
    with pytest.raises(ProviderTransientError):
        await partial_handler.execute(second_job, _Ctx())
    rows = await _acquisitions()
    assert len(rows) == 2
    partial = next(row for row in rows if row.manifest_sha256 != first.manifest_sha256)
    assert partial.expected_requests == ["table-a.json", "table-b.json"]
    assert partial.problems == [f"{HK_SOURCE}: transient_error: ProviderTransientError"]
    assert [item.content for item in verify_capture(tmp_path, partial.archive_ref).responses] == [b'{"ok":3}']
    assert partial_fetcher.calls[0].name == "table-a.json"


async def test_default_http_fetcher_archives_failed_http_body_before_data_error(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"issuer denied", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        fetcher = HttpxMaterialFetcher(client=client, min_interval={HK_SOURCE: 0.01})
        handler = MaterialCollectionHandler(
            settings=_settings(tmp_path),
            fetcher=fetcher,
            adapters={HK_SOURCE: ArchiveAdapter(names=("denied.json",))},
        )
        job, _ = await _claim(
            MaterialCollectionPayload(listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH)
        )
        assert await handler.execute(job, _Ctx()) == "partial"

    (acquisition,) = await _acquisitions()
    (response,) = verify_capture(tmp_path, acquisition.archive_ref).responses
    assert (response.http_status, response.content) == (403, b"issuer denied")
    assert response.parse_eligible is False
    assert acquisition.problems and "data_error" in acquisition.problems[0]


async def test_identical_observation_deduplicates_but_changed_fetch_time_adds_capture(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetched = datetime(2026, 10, 12, tzinfo=UTC)
    for batch, at in ((BATCH, fetched), ("2026-10-13", fetched), ("2026-10-14", fetched + timedelta(hours=1))):
        handler = MaterialCollectionHandler(
            settings=_settings(tmp_path),
            fetcher=ScriptedFetcher(responses={"income.json": _raw("income.json", b'{"same":1}', fetched_at=at)}),
            adapters={HK_SOURCE: ArchiveAdapter(names=("income.json",))},
        )
        job, _ = await _claim(
            MaterialCollectionPayload(listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=batch)
        )
        assert await handler.execute(job, _Ctx()) == "completed"

    acquisitions = await _acquisitions()
    assert len(acquisitions) == 2
    assert len({row.manifest_sha256 for row in acquisitions}) == 2
    assert await _dataset_count() == 1
