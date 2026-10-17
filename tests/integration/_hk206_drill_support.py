"""Shared machinery of the HK2-06 recovery drills (``test_hk206_recovery_drills``).

Everything here is simulation-only scaffolding for the drill tests: a
deterministic single-request adapter whose drafts are derived from the fetched
body (so a revised response honestly produces a revised fact), a scripted
fetcher that never opens a socket, and small query/copy helpers. No product
behaviour is changed; the drills exercise the real service chain
(queue -> handler -> store -> archive -> replay) with synthetic inputs only.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_EASTMONEY_HK,
    ControlledIdentity,
    load_pilot_identities,
)
from investment_agent.materials.contracts import (
    DatasetDraft,
    FactPeriod,
    ParseResult,
    RawPayload,
    UpstreamRequest,
)
from investment_agent.materials.handlers import _MaterialTransientError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.research_material import ResearchDataset
from tests.integration.test_material_collection_job import (
    HK_SOURCE,
    HSBC,
    SEC_SOURCE,
    _draft,
    _identity,
)
from tests.support.material_source_policy import synthetic_approved_source_policy
from tests.unit import test_material_source_probe as probe_fixtures

SIM_ADAPTER_VERSION = "hk206-sim-1"
"""Adapter version the synthetic drill adapters and approvals agree on."""

HK_REQUEST_NAME = "income.json"
SEC_REQUEST_NAME = "companyfacts.json"

PROBE_UA = probe_fixtures.GOOD_UA
"""A probe user agent that passes ``validate_user_agent`` (synthetic contact)."""


# --- deterministic synthetic responses -------------------------------------


def sim_body(amount: int) -> bytes:
    """One synthetic JSON response body; ``amount`` is the fact it will become."""

    return json.dumps({"amount": str(amount)}).encode("utf-8")


def sim_payload(name: str, amount: int, *, http_status: int = 200) -> RawPayload:
    return RawPayload(
        name=name,
        content=sim_body(amount),
        media_type="application/json",
        fetched_at=datetime.now(UTC),
        source_url=f"https://example.invalid/hk206/{name}",
        http_status=http_status,
    )


def transient_with_received_body(name: str, amount: int) -> _MaterialTransientError:
    """A transient HTTP 500 whose already-received error body is carried along.

    This is exactly what :class:`HttpxMaterialFetcher` raises on a 5xx: the
    handler must checkpoint the received response before the retry, so a drill
    can prove an interrupted source's partial evidence survives the restart.
    """

    raw = sim_payload(name, amount, http_status=500)
    return _MaterialTransientError(
        f"{name}: upstream HTTP 500",
        provider=SEC_SOURCE,
        error_code="http_500",
        raw_payload=raw,
        parse_eligible=False,
    )


# --- the synthetic adapter --------------------------------------------------


@dataclass
class SyntheticAnnualAdapter:
    """One request, one annual-facts draft derived from that response's body.

    ``parse`` turns ``{"amount": "1000"}`` into the ``raw_value`` of the draft
    fact, so a changed number in the response is a changed dataset (a new
    content hash), and an unchanged body is the same version no matter when it
    was fetched. Drafts are recorded for the store-level dedup drill.
    """

    source: str
    request_name: str = HK_REQUEST_NAME
    adapter_version: str = SIM_ADAPTER_VERSION
    drafts: list[DatasetDraft] = field(default_factory=list)
    subject_ids: list[int | None] = field(default_factory=list)

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        return (
            UpstreamRequest(
                name=self.request_name, url=f"https://example.invalid/hk206/{self.request_name}"
            ),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        self.subject_ids.append(subject_id)
        if not raw:
            return ParseResult(drafts=(), problems=())
        body = json.loads(raw[0].content)
        draft = replace(
            _draft(source=self.source, subject_id=subject_id, raw_value=str(body["amount"])),
            adapter_version=self.adapter_version,
        )
        self.drafts.append(draft)
        return ParseResult(drafts=(draft,), problems=())


# --- the scripted fetcher ---------------------------------------------------


@dataclass
class ScriptedFetcher:
    """Serves per-request outcome scripts (responses or exceptions), no socket.

    Each request name maps to the ordered outcomes its successive calls
    consume; a fetch beyond the script fails the test loudly instead of
    inventing traffic. Every call is recorded, so a drill can assert that a
    resumed attempt re-issued none of an already finished source's requests.
    """

    script: Mapping[str, list[RawPayload | Exception]]
    calls: list[str] = field(default_factory=list)

    async def fetch(self, request: UpstreamRequest, *, identity: ControlledIdentity) -> RawPayload:
        self.calls.append(request.name)
        outcomes = self.script[request.name]
        assert outcomes, f"{request.name}: scripted outcomes exhausted (unexpected extra fetch)"
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def count(self, name: str) -> int:
        return self.calls.count(name)


# --- approvals and read helpers ---------------------------------------------


def hsbc_two_source_approvals(
    adapter_version: str = SIM_ADAPTER_VERSION,
) -> dict[str, object]:
    """Synthetic policy approvals binding both HSBC sources to one version."""

    identity = _identity(HSBC)
    return {
        **synthetic_approved_source_policy(HK_SOURCE, identity, adapter_version),
        **synthetic_approved_source_policy(SEC_SOURCE, identity, adapter_version),
    }


async def dataset_rows() -> list[ResearchDataset]:
    async with session_scope() as session:
        return list((await session.execute(select(ResearchDataset))).scalars())


async def acquisition_rows() -> list[ResearchMaterialAcquisition]:
    async with session_scope() as session:
        return list((await session.execute(select(ResearchMaterialAcquisition))).scalars())


def dataset_signature(rows: Sequence[ResearchDataset]) -> tuple[tuple[Any, ...], ...]:
    """The drills' ``same facts and deterministic metrics`` comparison key.

    Timestamps (``captured_at`` / ``source_fetched_at``) are deliberately
    excluded: a replay may re-stamp its own clock, but the stored version
    identity (content hash), payload facts, revision chain and raw artifact
    reference must be identical.
    """

    return tuple(
        (
            row.id,
            row.revision,
            row.previous_id,
            row.content_hash,
            row.kind,
            row.source,
            row.subject_id,
            row.period_end,
            row.payload,
            row.raw_ref,
        )
        for row in rows
    )


def copy_sqlite(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)


# --- probe drills -----------------------------------------------------------


def one_hk_pilot_list(tmp_path: Path) -> tuple[Path, ControlledIdentity]:
    """A pilot list holding exactly one HK identity (fast, isolated probes)."""

    value: dict[str, Any] = yaml.safe_load(DEFAULT_PILOT_LIST_PATH.read_text(encoding="utf-8"))
    row = next(
        item
        for item in value["identities"]
        if any(binding["source"] == SOURCE_EASTMONEY_HK for binding in item["sources"])
    )
    value["identities"] = [row]
    pilot_path = tmp_path / "hk206-probe-pilot.yaml"
    pilot_path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    identity = next(
        item
        for item in load_pilot_identities(pilot_path)
        if any(binding.source == SOURCE_EASTMONEY_HK for binding in item.sources)
    )
    return pilot_path, identity


def probe_licensing_approval(identity: ControlledIdentity) -> dict[str, object]:
    """Licensing-only synthetic approval: a probe never needs technical acceptance."""

    from investment_agent.materials.hk_native import ADAPTER_VERSION

    approval = synthetic_approved_source_policy(SOURCE_EASTMONEY_HK, identity, ADAPTER_VERSION)
    record = approval[SOURCE_EASTMONEY_HK]
    assert isinstance(record, dict)
    return {
        SOURCE_EASTMONEY_HK: {
            "licensing": record["licensing"],
            "technical_acceptance": {"status": "not_accepted", "company_keys": [], "reports": []},
        }
    }


@dataclass
class ProbeStubFetcher:
    """The probe's view of a fetcher: no limiter wait, no socket, full call log.

    It honors the probe's ``before_send`` gate hook (the STOP / licence
    withdrawal checkpoint) and can run a callback during the would-be limiter
    wait, so a drill can drop a STOP file mid-flight exactly like the unit
    probe tests do.
    """

    user_agent: str
    identity: ControlledIdentity
    after_wait: Callable[[], None] | None = None
    before_send: Callable[[UpstreamRequest], Awaitable[object]] | None = None
    calls: list[str] = field(default_factory=list)

    async def fetch(self, request: UpstreamRequest, *, identity: ControlledIdentity) -> RawPayload:
        del identity
        await asyncio.sleep(0)
        if self.after_wait is not None:
            self.after_wait()
        assert self.before_send is not None, "the probe must install its gate hook"
        await self.before_send(request)
        self.calls.append(request.name)
        return RawPayload(
            name=request.name,
            content=probe_fixtures._response(self.identity, request),
            media_type="application/json",
            fetched_at=datetime.now(UTC),
            source_url=request.url,
            http_status=200,
        )
