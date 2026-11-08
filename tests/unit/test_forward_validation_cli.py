"""``ia forward-validation`` CLI: read-only opening, explicit insufficient exit.

These tests do not build a full PIT store; they pin the command's failure
modes, which are the ones a user hits first: a missing database file, an
unmigrated schema, and ``show`` reading a stored report without the database.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from investment_agent.research.forward_validation.runner import readonly_sqlite_url


def _invoke(*args: str):
    from investment_agent.cli.main import app

    return CliRunner().invoke(app, ["forward-validation", *args])


def test_readonly_url_rejects_a_missing_file(tmp_path: Path):
    missing = tmp_path / "absent.db"
    try:
        readonly_sqlite_url(missing)
    except FileNotFoundError as exc:
        assert "absent.db" in str(exc)
    else:  # pragma: no cover - the branch under test
        raise AssertionError("a missing database must not be opened or created")
    assert not missing.exists()


def test_readonly_url_uses_sqlite_mode_ro(tmp_path: Path):
    path = tmp_path / "app.db"
    sqlite3.connect(path).close()
    url = readonly_sqlite_url(path)
    assert "mode=ro" in url and "uri=true" in url


def test_cli_reports_insufficient_for_a_missing_database(tmp_path: Path):
    output = tmp_path / "report.json"
    result = _invoke(
        "run",
        "--database",
        str(tmp_path / "missing.db"),
        "--as-of",
        "2024-06-28",
        "--output",
        str(output),
    )
    assert result.exit_code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "insufficient"
    assert report["guard"]["calibration_status_changed"] is False
    assert report["database"]["read_only"] is True


def test_cli_reports_insufficient_for_an_unmigrated_schema(tmp_path: Path):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    output = tmp_path / "report.json"
    result = _invoke(
        "run",
        "--database",
        str(path),
        "--as-of",
        "2024-06-28",
        "--output",
        str(output),
    )
    assert result.exit_code == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "insufficient"
    assert report["database"]["path"] == str(path)


def test_cli_show_reads_a_stored_report(tmp_path: Path):
    output = tmp_path / "report.json"
    output.write_text(
        json.dumps(
            {
                "status": "insufficient",
                "insufficient_reasons": ["not enough data"],
                "sample": {"total_observations": 3, "total_scored": 2, "total_failed": 1},
                "config_hash": "abc123",
                "cross_sections": [],
            }
        ),
        encoding="utf-8",
    )
    result = _invoke("show", str(output))
    assert result.exit_code == 0
    assert "insufficient" in result.output
    assert "abc123" in result.output


def test_cli_rejects_an_unsupported_horizon(tmp_path: Path):
    path = tmp_path / "app.db"
    sqlite3.connect(path).close()
    result = _invoke(
        "run",
        "--database",
        str(path),
        "--as-of",
        "2024-06-28",
        "--horizon",
        "2",
    )
    assert result.exit_code != 0
    assert "unsupported horizon" in result.output
