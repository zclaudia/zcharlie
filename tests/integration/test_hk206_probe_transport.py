"""Root acceptance: actual fetcher/parser/archive/auditor with mock HTTP only."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_EASTMONEY_HK,
    load_pilot_identities,
)
from investment_agent.materials import source_probe
from investment_agent.materials.contracts import UpstreamRequest
from investment_agent.materials.handlers import HttpxMaterialFetcher
from investment_agent.materials.hk_native import HkNativeAdapter
from investment_agent.materials.source_observations import audit_observations
from tests.unit import test_material_source_probe as fixtures


def test_actual_transport_archive_and_offline_parser_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identities = [
        item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if any(binding.source == SOURCE_EASTMONEY_HK for binding in item.sources)
    ]
    expected: dict[str, tuple[object, UpstreamRequest]] = {}
    for identity in identities:
        for request in HkNativeAdapter().requests(identity, periods=fixtures._hk_periods()):
            key = str(httpx.URL(request.url, params=list(request.params)))
            expected[key] = (identity, request)
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == fixtures.GOOD_UA
        identity, upstream = expected[str(request.url)]
        seen.append(str(request.url))
        return httpx.Response(
            200,
            content=fixtures._response(identity, upstream, include_unmapped=True),
            headers={"content-type": "application/json"},
        )

    # No sockets: production HTTP serialization, shared limiter and parsers are
    # exercised with only the transport replaced. Keep the real limiter interval.
    def no_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("MockTransport acceptance must never open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    fetcher = HttpxMaterialFetcher(client=client, user_agent=fixtures.GOOD_UA)
    output_dir = tmp_path / "source-run"
    try:
        report = source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=output_dir,
            user_agent=fixtures.GOOD_UA,
            allow_network=True,
            periods=fixtures._hk_periods(),
            dependencies=source_probe.ProbeDependencies(
                approvals=fixtures._all_hk_approvals(identities),
                fetcher_factory=lambda _: fetcher,
            ),
        )
    finally:
        asyncio.run(client.aclose())
    assert report["status"] == "passed"
    assert report["mode"] == "simulation"
    assert report["requests_total"] == report["requests_succeeded"] == 9
    assert len(seen) == len(set(seen)) == 9
    assert len(report["archives"]) == 3
    assert len(report["parsed_facts"]) == 3
    assert report["issues"] == []
    assert report["informational"]
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "schema_version": 1,
        "source": SOURCE_EASTMONEY_HK,
        "adapter_version": HkNativeAdapter().adapter_version,
        "observation_reports": ["source-run/report.json"],
        "reviews": [],
    }), encoding="utf-8")
    result = audit_observations(ledger, tmp_path)
    assert result["reports"][0]["issues"] == []
    assert result["reports"][0]["passed"] is False  # simulation cannot qualify
    assert result["automatic_evidence"]["real_observations"] == 0
    assert result["decision"] == "pending"
    assert result["source_approval"] == "not_changed"
    assert result["global_enabled"] is False
