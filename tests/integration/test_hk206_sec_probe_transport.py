"""SEC probe integration with the actual fetcher/adapter and mock HTTP only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_SEC_COMPANYFACTS,
    load_pilot_identities,
    source_symbol,
)
from investment_agent.materials import source_probe
from investment_agent.materials.archive import verify_capture
from investment_agent.materials.handlers import HttpxMaterialFetcher
from investment_agent.materials.source_observations import audit_observations
from investment_agent.materials.us_sec import SHARD_URL, SecFactsAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "us"
USER_AGENT = "ZCharlie SEC probe integration test (contact probe@zcharlie.org)"
FIXTURE_BY_COMPANY = {
    "apple": "AAPL",
    "costco": "COST",
    "jpmorgan-chase": "JPM",
    "exxon-mobil": "XOM",
    "tsmc": "TSM",
    # HSBC is represented by a local fixture cloned from the TSM sample and
    # relabelled with HSBC's CIK. Every response remains explicitly synthetic.
    "hsbc-holdings": "TSM",
}


def _synthetic_source_approval(company_keys: list[str]) -> dict[str, object]:
    return {
        SOURCE_SEC_COMPANYFACTS: {
            "licensing": {
                "status": "approved",
                "company_keys": company_keys,
                "citation": "synthetic:integration-test/licensing",
            },
            "technical_acceptance": {
                "status": "not_accepted",
                "company_keys": [],
                "reports": [],
            },
        }
    }


def _read_company_fixture(company_key: str, cik: int) -> dict[str, bytes]:
    folder = FIXTURE_ROOT / FIXTURE_BY_COMPANY[company_key]
    docs: dict[str, bytes] = {}
    for name in ("companyfacts.json", "submissions.json"):
        value = json.loads((folder / name).read_text(encoding="utf-8"))
        value["cik"] = cik
        docs[name] = json.dumps(value, ensure_ascii=False).encode("utf-8")
    submissions = json.loads(docs["submissions.json"])
    for shard in submissions.get("filings", {}).get("files", []):
        name = shard["name"]
        raw = (folder / "shards" / name).read_bytes()
        docs[name] = raw
    return docs


def test_sec_real_adapter_probe_is_synthetic_simulation_and_audits_pending(
    tmp_path: Path, monkeypatch: Any
) -> None:
    identities = [
        identity
        for identity in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if source_symbol(identity, SOURCE_SEC_COMPANYFACTS) is not None
    ]
    assert set(identity.company_key for identity in identities) == set(FIXTURE_BY_COMPANY)

    response_by_url: dict[str, tuple[str, bytes]] = {}
    for identity in identities:
        cik = int(source_symbol(identity, SOURCE_SEC_COMPANYFACTS) or "0")
        docs = _read_company_fixture(identity.company_key, cik)
        adapter = SecFactsAdapter()
        for request in adapter.requests(identity):
            response_by_url[str(httpx.URL(request.url, params=list(request.params)))] = (
                request.name,
                docs[request.name],
            )
        for name in docs:
            if name not in {"companyfacts.json", "submissions.json"}:
                response_by_url[str(httpx.URL(SHARD_URL.format(name=name)))] = (name, docs[name])

    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        assert key in response_by_url, f"unexpected SEC request: {key}"
        name, body = response_by_url[key]
        seen.append((key, name))
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )

    def no_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("MockTransport test must never open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_socket)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    # Keep the SEC source's production limiter interval; this validates the
    # actual shared limiter behavior while the injected transport remains local.
    fetcher = HttpxMaterialFetcher(client=client, user_agent=USER_AGENT)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    report_dir = evidence_root / "sec-probe"
    try:
        report = cast(dict[str, Any], source_probe.run_source_probe(
            source=SOURCE_SEC_COMPANYFACTS,
            work_dir=report_dir,
            user_agent=USER_AGENT,
            allow_network=True,
            dependencies=source_probe.ProbeDependencies(
                approvals=_synthetic_source_approval([item.company_key for item in identities]),
                fetcher_factory=lambda _user_agent: fetcher,
            ),
        ))
    finally:
        asyncio.run(client.aclose())

    assert report["mode"] == "simulation"
    assert report["status"] == "passed"
    assert report["company_keys"] == sorted(FIXTURE_BY_COMPANY)
    assert report["requests_total"] == report["requests_succeeded"] == len(report["requests"])
    assert report["requests_failed"] == 0
    assert report["issues"] == []
    assert len(report["archives"]) == 6
    assert len(report["parsed_facts"]) == 6
    assert len(seen) == report["requests_total"]

    jpm_attempts = [
        row for row in report["requests"]
        if row["company_key"] == "jpmorgan-chase"
    ]
    assert {row["name"] for row in jpm_attempts} >= {
        "companyfacts.json",
        "submissions.json",
        "CIK0000019617-submissions-007.json",
        "CIK0000019617-submissions-035.json",
    }
    assert all(row["status"] == "http_2xx" for row in report["requests"])

    for archive in report["archives"]:
        capture = verify_capture(report_dir, archive["manifest_ref"], archive["manifest_sha256"])
        assert capture.identity["company_key"] == archive["company_key"]
        assert capture.subject_id > 0
        for response in capture.responses:
            assert hashlib.sha256(response.content).hexdigest()

    ledger_path = evidence_root / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": SOURCE_SEC_COMPANYFACTS,
                "adapter_version": SecFactsAdapter().adapter_version,
                "observation_reports": ["sec-probe/report.json"],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    audited = audit_observations(ledger_path, evidence_root)
    assert audited["reports"][0]["issues"] == []
    assert audited["reports"][0]["passed"] is False
    assert audited["automatic_evidence"]["real_observations"] == 0
    assert audited["decision"] == "pending"
    assert audited["source_approval"] == "not_changed"
    assert audited["global_enabled"] is False

    assert datetime.fromisoformat(str(report["observed_at"])).tzinfo is not None
