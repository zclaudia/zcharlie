"""Contract tests for the HK2-06 drill evidence schema (v1).

Positive and negative cases for :mod:`investment_agent.materials.drill_evidence`
: the six drill names must mirror the acceptor's vocabulary, malformed records
must fail closed, artifact hashes must actually be verified on load, and
simulation records may never claim network use.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from investment_agent.materials import source_observations
from investment_agent.materials.drill_evidence import (
    DRILL_NAMES,
    EVIDENCE_FILENAME,
    DrillEvidenceError,
    load_drill_evidence,
    validate_drill_evidence,
    write_drill_evidence,
)

HK_SOURCE = "eastmoney.hk.native.v1"
SEC_SOURCE = "sec.companyfacts.v1"


def _artifact(root: Path, name: str = "records.json", content: bytes = b"drill artifact\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _minimal_evidence(root: Path, **overrides: Any) -> dict[str, Any]:
    artifact = _artifact(root)
    value: dict[str, Any] = {
        "schema_version": 1,
        "drill": "shutdown",
        "mode": "simulation",
        "source": HK_SOURCE,
        "adapter_version": "1",
        "executed_at": datetime.now(UTC).isoformat(),
        "isolated_environment": {
            "database_uri": "sqlite+aiosqlite:///:memory:",
            "data_dir": str(root / "data"),
        },
        "service_chain": ["investment_agent.materials.handlers.MaterialCollectionHandler"],
        "company_scope": ["clp-holdings", "hsbc-holdings", "tencent-holdings"],
        "steps": [
            {
                "action": "close the switch",
                "expected": "no fetch starts",
                "observed": "zero requests",
                "passed": True,
            }
        ],
        "requests": [],
        "artifacts": [
            {
                "kind": "worker_run_records",
                "path": artifact.name,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ],
        "network_calls": 0,
        "stop_semantics": {
            "stop_mechanism": "operational source-refresh switch closed",
            "requests_after_stop": 0,
            "received_responses_retained": True,
        },
        "passed": True,
        "failure_reasons": [],
    }
    value.update(overrides)
    return value


def _invalid(value: dict[str, Any]) -> None:
    with pytest.raises(DrillEvidenceError):
        validate_drill_evidence(dict(value))


def test_minimal_simulation_evidence_round_trips(tmp_path: Path) -> None:
    directory = tmp_path / "drill-dir"
    value = _minimal_evidence(directory)
    evidence = validate_drill_evidence(value)
    assert evidence.drill == "shutdown"
    assert evidence.mode == "simulation"
    assert evidence.network_calls == 0
    assert evidence.passed is True
    assert evidence.stop_semantics is not None
    assert evidence.stop_semantics.received_responses_retained is True

    written = write_drill_evidence(directory, evidence)
    assert written.name == EVIDENCE_FILENAME
    loaded = load_drill_evidence(written)
    assert loaded == evidence
    assert loaded.to_mapping() == json.loads(written.read_text(encoding="utf-8"))
    # a second record in another directory keeps its own artifact copy
    other_dir = tmp_path / "drill-dir-2"
    other = validate_drill_evidence(_minimal_evidence(other_dir))
    assert load_drill_evidence(write_drill_evidence(other_dir, other)) == other


def test_real_mode_is_accepted_with_network_calls(tmp_path: Path) -> None:
    value = _minimal_evidence(
        tmp_path,
        mode="real",
        network_calls=3,
        requests=[
            {
                "company_key": "hsbc-holdings",
                "name": "income",
                "http_status": 200,
                "transport": "https",
            }
        ],
    )
    evidence = validate_drill_evidence(value)
    assert (evidence.mode, evidence.network_calls) == ("real", 3)


def test_drill_names_mirror_the_acceptor_vocabulary() -> None:
    assert DRILL_NAMES == source_observations._DRILLS
    assert set(DRILL_NAMES) == {
        "shutdown",
        "inflight_stop",
        "restart_resume",
        "old_versions_readable",
        "offline_replay",
        "probe_stop_withdrawal",
    }


def test_company_scope_mirrors_the_acceptor_controlled_sets(tmp_path: Path) -> None:
    evidence = validate_drill_evidence(_minimal_evidence(tmp_path))
    assert evidence.company_scope == source_observations.SOURCE_COMPANIES[HK_SOURCE]


# --- negative cases ----------------------------------------------------------


def test_missing_field_is_rejected(tmp_path: Path) -> None:
    for key in (
        "schema_version",
        "drill",
        "mode",
        "source",
        "adapter_version",
        "executed_at",
        "isolated_environment",
        "service_chain",
        "company_scope",
        "steps",
        "requests",
        "artifacts",
        "network_calls",
        "passed",
        "failure_reasons",
        "stop_semantics",
    ):
        value = _minimal_evidence(tmp_path)
        del value[key]
        with pytest.raises(DrillEvidenceError):
            validate_drill_evidence(value)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, extra_note="not in the contract"))


def test_wrong_schema_version_and_string_version_are_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, schema_version=2))
    _invalid(_minimal_evidence(tmp_path, schema_version="1"))


def test_boolean_schema_version_is_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, schema_version=True))


def test_boolean_integers_everywhere_are_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, network_calls=True))
    value = _minimal_evidence(tmp_path)
    value["requests"] = [
        {"company_key": "hsbc-holdings", "name": "income", "http_status": True, "transport": "mock"}
    ]
    _invalid(value)
    value = _minimal_evidence(tmp_path)
    value["stop_semantics"]["requests_after_stop"] = True
    _invalid(value)


def test_misspelled_drill_name_is_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, drill="shutdow"))


def test_unknown_mode_and_source_are_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, mode="dry_run"))
    _invalid(_minimal_evidence(tmp_path, source="yahoo.chart.v1"))


def test_naive_and_future_executed_at_are_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, executed_at="2026-10-17T10:00:00"))
    _invalid(_minimal_evidence(tmp_path, executed_at="not-a-timestamp"))
    _invalid(
        _minimal_evidence(
            tmp_path, executed_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
        )
    )


def test_isolated_environment_must_carry_both_non_empty_paths(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, isolated_environment={"database_uri": "x"}))
    _invalid(
        _minimal_evidence(
            tmp_path,
            isolated_environment={"database_uri": "x", "data_dir": " ", "extra": 1},
        )
    )


def test_empty_service_chain_company_scope_and_steps_are_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, service_chain=[]))
    _invalid(_minimal_evidence(tmp_path, company_scope=[]))
    _invalid(_minimal_evidence(tmp_path, steps=[]))


def test_uncontrolled_company_scope_is_rejected(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, company_scope=["apple", "costco"]))
    _invalid(_minimal_evidence(tmp_path, company_scope=["clp-holdings", "clp-holdings"]))
    _invalid(
        _minimal_evidence(
            tmp_path,
            source=SEC_SOURCE,
            company_scope=["apple", "costco", "jpmorgan-chase", "exxon-mobil", "tsmc",
                           "hsbc-holdings", "clp-holdings"],
        )
    )


def test_simulation_must_claim_zero_network_and_mock_transport(tmp_path: Path) -> None:
    _invalid(_minimal_evidence(tmp_path, network_calls=1))
    value = _minimal_evidence(tmp_path)
    value["requests"] = [
        {"company_key": "hsbc-holdings", "name": "income", "http_status": 200, "transport": "https"}
    ]
    _invalid(value)


def test_request_company_outside_the_source_range_is_rejected(tmp_path: Path) -> None:
    value = _minimal_evidence(tmp_path)
    value["requests"] = [
        {"company_key": "apple", "name": "income", "http_status": 200, "transport": "mock"}
    ]
    _invalid(value)


def test_passed_must_match_steps_and_failure_reasons(tmp_path: Path) -> None:
    failed_step = {
        "action": "stop the source",
        "expected": "zero requests after the stop",
        "observed": "one request was still issued",
        "passed": False,
    }
    value = _minimal_evidence(tmp_path)
    value["steps"] = [failed_step]
    _invalid(value)  # passed=True contradicts a failed step
    value = _minimal_evidence(tmp_path)
    value["failure_reasons"] = ["requests_after_stop was 1"]
    _invalid(value)
    recovered = validate_drill_evidence(
        _minimal_evidence(tmp_path, steps=[failed_step], passed=False,
                          failure_reasons=["requests_after_stop was 1"])
    )
    assert recovered.passed is False


def test_stop_semantics_are_only_for_stopping_drills(tmp_path: Path) -> None:
    value = _minimal_evidence(tmp_path, drill="restart_resume")
    del value["stop_semantics"]
    assert validate_drill_evidence(value).stop_semantics is None
    # carrying one (even an explicit null) on a non-stopping drill is rejected
    _invalid(_minimal_evidence(tmp_path, drill="restart_resume"))
    value = _minimal_evidence(tmp_path, drill="restart_resume")
    value["stop_semantics"] = None
    _invalid(value)
    # ... while a stopping drill must carry one
    value = _minimal_evidence(tmp_path, drill="inflight_stop")
    del value["stop_semantics"]
    _invalid(value)


def test_stop_semantics_unknown_keys_and_negative_counts_are_rejected(tmp_path: Path) -> None:
    value = _minimal_evidence(tmp_path)
    value["stop_semantics"] = {
        "stop_mechanism": "STOP file",
        "requests_after_stop": 0,
        "received_responses_retained": True,
        "why": "not in the contract",
    }
    _invalid(value)
    value["stop_semantics"] = {"stop_mechanism": "STOP file", "requests_after_stop": -1}
    _invalid(value)


# --- artifact path safety and hash binding -----------------------------------


def test_unsafe_artifact_paths_are_rejected(tmp_path: Path) -> None:
    for unsafe in ("../escape.json", "/absolute/path.json", "a/../../b.json", "back\\slash.json"):
        value = _minimal_evidence(tmp_path)
        value["artifacts"] = [{"kind": "x", "path": unsafe,
                               "sha256": "0" * 64}]
        _invalid(value)


def test_artifact_sha256_must_be_a_digest(tmp_path: Path) -> None:
    value = _minimal_evidence(tmp_path)
    value["artifacts"] = [{"kind": "x", "path": "records.json", "sha256": "nothex"}]
    _invalid(value)


def test_load_verifies_artifact_hashes(tmp_path: Path) -> None:
    directory = tmp_path / "drill"
    evidence = validate_drill_evidence(_minimal_evidence(directory))
    written = write_drill_evidence(directory, evidence)
    assert load_drill_evidence(written) == evidence

    artifact = directory / "records.json"
    original = artifact.read_bytes()
    artifact.write_bytes(original + b"tampered")
    with pytest.raises(DrillEvidenceError, match="sha256 mismatch"):
        load_drill_evidence(written)
    artifact.write_bytes(original)
    assert load_drill_evidence(written) == evidence


def test_load_refuses_symlinked_artifacts_and_missing_files(tmp_path: Path) -> None:
    directory = tmp_path / "drill"
    evidence = validate_drill_evidence(_minimal_evidence(directory))
    written = write_drill_evidence(directory, evidence)
    (directory / "records.json").unlink()
    with pytest.raises(DrillEvidenceError):
        load_drill_evidence(written)

    other = tmp_path / "other.json"
    other.write_bytes(b"elsewhere")
    link_dir = tmp_path / "drill-2"
    evidence = validate_drill_evidence(
        {
            **_minimal_evidence(link_dir),
            "artifacts": [
                {
                    "kind": "x",
                    "path": "link.json",
                    "sha256": hashlib.sha256(b"elsewhere").hexdigest(),
                }
            ],
        }
    )
    written = write_drill_evidence(link_dir, evidence)
    os.symlink(other, link_dir / "link.json")
    with pytest.raises(DrillEvidenceError, match="symlink"):
        load_drill_evidence(written)


def test_load_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(DrillEvidenceError):
        load_drill_evidence(path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DrillEvidenceError):
        load_drill_evidence(path)
    with pytest.raises(DrillEvidenceError):
        load_drill_evidence(tmp_path / "missing.json")


def test_write_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    directory = tmp_path / "drill"
    evidence = validate_drill_evidence(_minimal_evidence(tmp_path))
    write_drill_evidence(directory, evidence)
    with pytest.raises(DrillEvidenceError):
        write_drill_evidence(directory, evidence)
