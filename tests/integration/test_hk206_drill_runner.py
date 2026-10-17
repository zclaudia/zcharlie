"""End-to-end tests for the HK2-06 drill CLI (simulation only, real chains).

The CLI runs in a hostile-environment subprocess (dotenv present, sentinel
``DATABASE_URL`` / ``DATA_DIR``); every drill must pass, its evidence must
validate and hash-check, the isolated scratch environment must stay inside the
evidence root, and the generated citations must line up with what
``source_observations.audit_observations`` accepts in a manual review — while a
simulation report can never advance the decision past ``pending``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from investment_agent.materials.drill_evidence import (
    DRILL_NAMES,
    STOP_SEMANTICS_DRILLS,
    DrillEvidenceError,
    load_drill_evidence,
)
from investment_agent.materials.source_observations import (
    SOURCE_COMPANIES,
    audit_observations,
)
from tests.integration import _hk206_drill_cli_support as cli_support
from tests.unit import test_material_source_observations as observation_fixtures

HK_SOURCE = "eastmoney.hk.native.v1"
SEC_SOURCE = "sec.companyfacts.v1"


@pytest.fixture(scope="module")
def hk_run(tmp_path_factory: pytest.TempPathFactory) -> cli_support.DrillCliRun:
    run = cli_support.run_drill_cli(tmp_path_factory.mktemp("hk206-drill-hk"), HK_SOURCE)
    assert run.completed.returncode == 0, run.completed.stderr
    return run


@pytest.fixture(scope="module")
def sec_run(tmp_path_factory: pytest.TempPathFactory) -> cli_support.DrillCliRun:
    run = cli_support.run_drill_cli(tmp_path_factory.mktemp("hk206-drill-sec"), SEC_SOURCE)
    assert run.completed.returncode == 0, run.completed.stderr
    return run


@pytest.mark.parametrize("source_run", ["hk", "sec"])
def test_cli_runs_all_six_drills_with_valid_evidence(
    source_run: str,
    hk_run: cli_support.DrillCliRun,
    sec_run: cli_support.DrillCliRun,
) -> None:
    run = hk_run if source_run == "hk" else sec_run
    source = HK_SOURCE if source_run == "hk" else SEC_SOURCE
    summary = run.summary

    assert summary["schema_version"] == 1
    assert summary["source"] == source
    assert summary["mode"] == "simulation"
    assert summary["network_calls"] == 0
    assert sorted(summary["drills"]) == sorted(DRILL_NAMES)
    assert all(entry["passed"] for entry in summary["drills"].values())

    for drill, entry in summary["drills"].items():
        evidence_path = run.evidence_root / str(entry["evidence_path"])
        assert evidence_path.is_file(), drill
        assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == entry["sha256"]
        evidence = load_drill_evidence(evidence_path)
        assert evidence.passed is True
        assert (evidence.drill, evidence.mode, evidence.source) == (drill, "simulation", source)
        assert evidence.network_calls == 0
        assert evidence.company_scope == SOURCE_COMPANIES[source]
        assert evidence.service_chain
        if drill in STOP_SEMANTICS_DRILLS:
            assert evidence.stop_semantics is not None
            assert evidence.stop_semantics.requests_after_stop == 0
        else:
            assert evidence.stop_semantics is None
        for request in evidence.requests:
            assert request.transport == "mock"
            # restart_resume scripts one transient HTTP 500 as its interruption;
            # every other drill only sees successful mock responses.
            if drill == "restart_resume":
                assert request.http_status in (200, 500)
            else:
                assert request.http_status == 200

    probe_entry = summary["drills"]["probe_stop_withdrawal"]
    withdrawal_path = run.evidence_root / str(
        probe_entry["licence_withdrawal_evidence_path"]
    )
    assert hashlib.sha256(withdrawal_path.read_bytes()).hexdigest() == (
        probe_entry["licence_withdrawal_sha256"]
    )
    withdrawal = load_drill_evidence(withdrawal_path)
    assert withdrawal.stop_semantics is not None
    assert withdrawal.stop_semantics.stop_mechanism != (
        load_drill_evidence(run.evidence_root / str(probe_entry["evidence_path"]))
        .stop_semantics
        .stop_mechanism
    )

    # The isolated database/data dir of every drill stayed inside the root.
    for entry in summary["drills"].values():
        value = json.loads(
            (run.evidence_root / str(entry["evidence_path"])).read_text(encoding="utf-8")
        )
        scratch = run.evidence_root / "scratch"
        assert Path(value["isolated_environment"]["data_dir"]).is_relative_to(scratch)
        assert Path(value["isolated_environment"]["database_uri"].removeprefix(
            "sqlite+aiosqlite:///"
        )).is_relative_to(scratch)

    assert cli_support.sentinels_unchanged(run)


def test_existing_evidence_root_is_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / "evidence").mkdir()
    run = cli_support.run_drill_cli(tmp_path, HK_SOURCE)
    assert run.completed.returncode == 2
    assert "must not already exist" in run.completed.stderr
    assert list((tmp_path / "evidence").iterdir()) == []


def test_a_tampered_artifact_byte_fails_the_evidence_load(
    hk_run: cli_support.DrillCliRun,
) -> None:
    summary = hk_run.summary
    entry = summary["drills"]["shutdown"]
    evidence_path = hk_run.evidence_root / str(entry["evidence_path"])
    evidence = load_drill_evidence(evidence_path)
    (artifact,) = evidence.artifacts
    target = evidence_path.parent / artifact.path
    original = target.read_bytes()
    target.write_bytes(original + b"tampered")
    with pytest.raises(DrillEvidenceError):
        load_drill_evidence(evidence_path)
    target.write_bytes(original)
    assert load_drill_evidence(evidence_path) == evidence


def test_generated_drill_citations_satisfy_the_acceptor_review_contract(
    sec_run: cli_support.DrillCliRun,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drill evidence feeds the acceptor's manual-review drill citations.

    A fully-cited review of a simulation report is review-ready per report, but
    the audit decision itself stays ``pending`` with zero real observations;
    a missing citation or a wrong hash must break that readiness without ever
    upgrading the decision.
    """

    monkeypatch.setattr(
        observation_fixtures.SecFactsAdapter,
        "parse",
        observation_fixtures._fake_parse,
    )
    summary = sec_run.summary
    # The generated drill evidence must live inside the audit's evidence root;
    # copy the produced tree verbatim (real files, byte-identical hashes).
    shutil.copytree(sec_run.evidence_root / "drills", tmp_path / "drills")
    drill_citations = {
        drill: {
            "status": "passed",
            "citation": {
                "path": str(entry["evidence_path"]),
                "sha256": str(entry["sha256"]),
            },
        }
        for drill, entry in summary["drills"].items()
    }

    def build_review(drills: dict[str, Any]) -> dict[str, Any]:
        citation_path = tmp_path / "reviews" / "human-review.md"
        citation_path.parent.mkdir(parents=True, exist_ok=True)
        citation_path.write_text("local reviewed evidence\n", encoding="utf-8")
        citation = {
            "path": "reviews/human-review.md",
            "sha256": hashlib.sha256(citation_path.read_bytes()).hexdigest(),
        }
        return {
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "reviewer": "manual-reviewer",
            "reviewed_at": datetime.now(UTC).isoformat(),
            "annual_comparison": {
                "status": "matched",
                "expected_periods": [],
                "citation": citation,
            },
            "source_scope_review": {"status": "approved", "citation": citation},
            "drills": drills,
        }

    report_path = observation_fixtures._make_report(tmp_path, 10, mode="simulation")
    ledger_path = observation_fixtures._ledger(
        tmp_path, [report_path], [build_review(drill_citations)]
    )
    result = audit_observations(ledger_path, tmp_path)
    assert result["reports"][0]["manual_review"]["ready"] is True
    assert result["reports"][0]["passed"] is False  # simulation never qualifies
    assert result["automatic_evidence"]["real_observations"] == 0
    assert result["decision"] == "pending"
    assert result["source_approval"] == "not_changed"
    assert result["global_enabled"] is False

    missing = {name: value for name, value in drill_citations.items() if name != "offline_replay"}
    ledger_missing = observation_fixtures._ledger(
        tmp_path, [report_path], [build_review(missing)]
    )
    result_missing = audit_observations(ledger_missing, tmp_path)
    assert result_missing["reports"][0]["manual_review"]["ready"] is False
    assert any(
        "offline_replay drill is pending" in issue
        for issue in result_missing["reports"][0]["manual_review"]["issues"]
    )
    assert result_missing["decision"] == "pending"
    assert result_missing["automatic_evidence"]["real_observations"] == 0

    wrong_hash = json.loads(json.dumps(drill_citations))
    wrong_hash["restart_resume"]["citation"]["sha256"] = "0" * 64
    ledger_wrong = observation_fixtures._ledger(
        tmp_path, [report_path], [build_review(wrong_hash)]
    )
    result_wrong = audit_observations(ledger_wrong, tmp_path)
    assert result_wrong["reports"][0]["manual_review"]["ready"] is False
    assert any(
        "restart_resume citation is missing, unsafe, or hash-mismatched" in issue
        for issue in result_wrong["reports"][0]["manual_review"]["issues"]
    )
    assert result_wrong["decision"] == "pending"
