from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from investment_agent.domain.security import SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS
from investment_agent.materials import source_probe
from investment_agent.materials.handlers import ONLINE_REFRESH_ENABLED
from investment_agent.materials.source_observations import (
    ObservationAuditError,
    audit_observations,
)
from tests.unit import test_material_source_observations as observation_fixtures
from tests.unit import test_material_source_probe as probe_fixtures

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_POLICY_PATH = REPO_ROOT / "config/research_material/source_policy.yaml"
VALID_UA = "ZCharlie integration guard (contact: integration-guard@zcharlie.org)"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_socketguard(path: Path) -> None:
    path.mkdir()
    (path / "sitecustomize.py").write_text(
        """
import socket
import sys

def _guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
        raise RuntimeError("socketguard: network access is forbidden in this subprocess")
    if event == "open" and args:
        value = args[0]
        if isinstance(value, (str, bytes)) and str(value).endswith(".env"):
            raise RuntimeError("socketguard: .env reads are forbidden")

sys.addaudithook(_guard)

def _forbid(*args, **kwargs):
    raise RuntimeError("socketguard: network access is forbidden in this subprocess")

socket.create_connection = _forbid
socket.socket.connect = _forbid
socket.socket.connect_ex = _forbid
""",
        encoding="utf-8",
    )


def test_injected_approval_without_fetcher_never_falls_back_to_real_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pilot_path, identity = probe_fixtures._one_hk_identity(tmp_path)
    dependencies = source_probe.ProbeDependencies(
        approvals=probe_fixtures._approval(identity),
        pilot_list_path=pilot_path,
        # Deliberately no fetcher_factory: injected approvals must not enable
        # the production HttpxMaterialFetcher fallback.
    )
    send_attempts: list[str] = []
    connect_attempts: list[str] = []

    async def forbidden_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> Any:
        send_attempts.append(str(request.url))
        raise RuntimeError("forbidden test HTTP send")

    original_connect = socket.socket.connect

    def forbidden_connect(self: socket.socket, address: Any) -> Any:
        connect_attempts.append(str(address))
        raise RuntimeError("forbidden test socket connect")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
    policy_hash = _sha256(SOURCE_POLICY_PATH)
    assert ONLINE_REFRESH_ENABLED is False

    output = tmp_path / "probe-output"
    try:
        report = source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=output,
            user_agent=VALID_UA,
            allow_network=True,
            dependencies=dependencies,
            periods=probe_fixtures._hk_periods(),
        )
    except RuntimeError as exc:
        pytest.fail(f"probe attempted its default network client: {exc}")
    finally:
        monkeypatch.setattr(socket.socket, "connect", original_connect)

    assert send_attempts == []
    assert connect_attempts == []
    assert report["mode"] == "simulation"
    assert report["requests_total"] == 0
    assert report["status"] == "blocked"
    assert any(
        str(issue).startswith("simulation_fetcher_required:")
        for issue in report["issues"]
    )
    assert _sha256(SOURCE_POLICY_PATH) == policy_hash
    assert ONLINE_REFRESH_ENABLED is False


@pytest.mark.parametrize(
    "source",
    (SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS),
)
def test_cli_dry_runs_are_network_and_environment_isolated(
    tmp_path: Path, source: str
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    guard_dir = tmp_path / "guard"
    _write_socketguard(guard_dir)

    dotenv = cwd / ".env"
    dotenv.write_text(
        "API_KEY=dotenv-must-not-be-read\n"
        "ONLINE_REFRESH_ENABLED=true\n"
        f"DATABASE_URL=sqlite:///{tmp_path / 'dotenv-database.sqlite'}\n",
        encoding="utf-8",
    )
    dotenv_hash = _sha256(dotenv)
    database_sentinel = tmp_path / "database-sentinel.sqlite"
    database_sentinel.write_bytes(b"database sentinel: must remain byte-identical\n")
    database_hash = _sha256(database_sentinel)
    data_dir = tmp_path / "data-sentinel"
    data_dir.mkdir()
    (data_dir / "keep.txt").write_text("data sentinel\n", encoding="utf-8")
    data_snapshot = {
        path.relative_to(data_dir).as_posix(): _sha256(path)
        for path in data_dir.rglob("*")
        if path.is_file()
    }
    policy_hash = _sha256(SOURCE_POLICY_PATH)
    work_dir = tmp_path / f"new-work-{source.replace('.', '-')}"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(guard_dir), str(REPO_ROOT / "src"))),
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_sentinel}",
        "DATA_DIR": str(data_dir),
        "API_KEY": "integration-fake-api-key",
        "LLM_TIER3_API_KEY": "integration-fake-llm-key",
        "ONLINE_REFRESH_ENABLED": "true",
    }
    command = [
        sys.executable,
        str(REPO_ROOT / "tools/hk206_source_probe.py"),
        "--source",
        source,
        "--work-dir",
        str(work_dir),
        "--user-agent",
        VALID_UA,
    ]
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry_run"
    assert report["status"] == "dry_run"
    assert report["requests_total"] == 0
    assert report["requests"] == []
    assert (work_dir / "report.json").is_file()
    assert sorted(path.relative_to(work_dir).as_posix() for path in work_dir.rglob("*") if path.is_file()) == [
        "report.json"
    ]
    assert _sha256(dotenv) == dotenv_hash
    assert _sha256(database_sentinel) == database_hash
    assert {
        path.relative_to(data_dir).as_posix(): _sha256(path)
        for path in data_dir.rglob("*")
        if path.is_file()
    } == data_snapshot
    assert _sha256(SOURCE_POLICY_PATH) == policy_hash
    assert ONLINE_REFRESH_ENABLED is False


def test_injected_multiday_simulations_cannot_count_as_real_observations_or_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_hash = _sha256(SOURCE_POLICY_PATH)
    monkeypatch.setattr(
        observation_fixtures.SecFactsAdapter,
        "parse",
        observation_fixtures._fake_parse,
    )
    reports: list[Path] = []
    reviews: list[dict[str, object]] = []
    for day in (10, 11, 12):
        report = observation_fixtures._make_report(
            tmp_path, day, mode="simulation", status="passed"
        )
        reports.append(report)
        reviews.append(observation_fixtures._add_review(report, tmp_path))

    result = audit_observations(
        observation_fixtures._ledger(tmp_path, reports, reviews), tmp_path
    )

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False
    assert result["source_approval"] == "not_changed"
    assert result["global_enabled"] is False
    assert _sha256(SOURCE_POLICY_PATH) == policy_hash
    assert ONLINE_REFRESH_ENABLED is False


def test_malformed_ledger_source_type_is_rejected_without_typeerror_trace(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": [],
                "adapter_version": "1",
                "observation_reports": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ObservationAuditError):
        audit_observations(ledger, tmp_path)


def test_boolean_schema_version_is_never_candidate(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": True,
                "source": SOURCE_SEC_COMPANYFACTS,
                "adapter_version": "1",
                "observation_reports": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    try:
        result = audit_observations(ledger, tmp_path)
    except ObservationAuditError:
        return
    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False


def test_nested_company_keys_are_rejected_by_cli_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        observation_fixtures.SecFactsAdapter,
        "parse",
        observation_fixtures._fake_parse,
    )
    report = observation_fixtures._make_report(tmp_path, 10)
    value = json.loads(report.read_text(encoding="utf-8"))
    value["company_keys"] = [[]]
    report.write_text(json.dumps(value), encoding="utf-8")
    review = observation_fixtures._add_review(report, tmp_path)
    ledger = observation_fixtures._ledger(tmp_path, [report], [review])

    from tools.hk206_source_acceptance import main as acceptance_main

    try:
        acceptance_main(
            [
                "--ledger",
                str(ledger),
                "--evidence-root",
                str(tmp_path),
            ]
        )
    except SystemExit as exit_info:
        assert exit_info.code == 2
    else:
        assert json.loads(capsys.readouterr().out)["decision"] == "pending"

    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_nan_duration_cannot_become_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_hash = _sha256(SOURCE_POLICY_PATH)
    monkeypatch.setattr(
        observation_fixtures.SecFactsAdapter,
        "parse",
        observation_fixtures._fake_parse,
    )
    reports: list[Path] = []
    reviews: list[dict[str, object]] = []
    for day in (10, 11, 12):
        report = observation_fixtures._make_report(tmp_path, day)
        value = json.loads(report.read_text(encoding="utf-8"))
        value["duration_seconds"] = float("nan")
        report.write_text(json.dumps(value), encoding="utf-8")
        reports.append(report)
        reviews.append(observation_fixtures._add_review(report, tmp_path))

    result = audit_observations(
        observation_fixtures._ledger(tmp_path, reports, reviews), tmp_path
    )

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False
    assert all(
        any("duration_seconds" in issue for issue in report["issues"])
        for report in result["reports"]
    )
    assert _sha256(SOURCE_POLICY_PATH) == policy_hash
    assert ONLINE_REFRESH_ENABLED is False
