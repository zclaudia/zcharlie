"""End-to-end offline HK2-08 M1 recovery drill and focused failure evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from investment_agent.materials.annual_validation import verify_annual_package
from tests.unit.test_annual_validation import _make_package


def test_m1_drill_migrates_imports_restores_and_checks_offline_reads(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    manifest, source_root, _ = _make_package(inputs)
    repo = Path(__file__).resolve().parents[2]
    work_dir = tmp_path / "m1-drill"
    env = os.environ.copy()
    # Hostile external paths prove that the drill pins every writable location
    # to its required work directory.
    env.update(
        {
            "PYTHONPATH": str(repo / "src"),
            "DATABASE_URL": "sqlite+aiosqlite:////must-not-be-used/production.db",
            "DATA_DIR": "/must-not-be-used/production-data",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(repo / "tools/hk208_m1_drill.py"),
            "--work-dir",
            str(work_dir),
            "--manifest",
            str(manifest),
            "--source-root",
            str(source_root),
            "--source-repo",
            str(repo),
        ],
        env=env,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    report_path = work_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed", completed.stdout
    assert report["scope"]["production_release"] is False
    assert report["scope"]["database_migration"] == "isolated drill database only"
    assert report["scope"]["reviewed_import"] == "isolated drill database only"
    assert report["scope"]["source_mode"] == (
        "offline-only; handlers.ONLINE_REFRESH_ENABLED=False (asserted)"
    )
    assert report["scope"]["browser_page_acceptance"].startswith("not checked by this CLI")
    assert report["network_guard"]["parent_attempts_observed"] == 0
    assert report["network_guard"]["subprocess_attempts_observed"] == 0
    assert report["migration"] == {"from": "048", "to": "049", "backup_before_upgrade": True}
    assert report["reviewed_import_and_calculations"]["companies_imported"] == 8
    assert report["reviewed_import_and_calculations"]["datasets_created"] == 8
    assert report["reviewed_import_and_calculations"]["datasets_reused_on_repeat"] == 8
    restored = Path(report["paths"]["work_dir"]) / "restored"
    assert all(
        status == 200 for status in report["restored_read_validation"]["http_statuses"].values()
    )
    company_calculation_counts = {
        company: len(refs)
        for company, refs in report["reviewed_import_and_calculations"]["prepared"].items()
    }
    assert len(company_calculation_counts) == 8
    assert all(count >= 1 for count in company_calculation_counts.values())
    assert report["restored_read_validation"]["calculation_count"] == sum(
        company_calculation_counts.values()
    )
    assert report["restored_read_validation"]["legacy_sentinel_preserved"] is True
    assert (restored / "app.db").is_file()
    for path_key in ("work_dir",):
        assert Path(report["paths"][path_key]).resolve() == work_dir.resolve()

    # Missing originals and changed bytes must make package verification fail.
    deleted_root = tmp_path / "deleted-originals"
    shutil.copytree(source_root, deleted_root)
    first_original = next(path for path in deleted_root.rglob("*") if path.is_file())
    first_original.unlink()
    deleted_check = verify_annual_package(manifest, deleted_root)
    assert not deleted_check["complete"]

    changed_root = tmp_path / "changed-originals"
    shutil.copytree(source_root, changed_root)
    first_original = next(path for path in changed_root.rglob("*") if path.is_file())
    first_original.write_bytes(first_original.read_bytes() + b"\ntampered")
    changed_check = verify_annual_package(manifest, changed_root)
    assert not changed_check["complete"]

    # A restored database paired with an empty source archive is an invalid
    # release check. The check runs read-only in a separate interpreter.
    bad_data_dir = tmp_path / "wrong-originals"
    bad_data_dir.mkdir()
    mismatch_report = tmp_path / "mismatch-report.json"
    mismatch = subprocess.run(
        [
            sys.executable,
            str(repo / "tools/hk208_release_check.py"),
            "--database",
            str(restored / "app.db"),
            "--data-dir",
            str(bad_data_dir),
            "--report",
            str(mismatch_report),
            "--require-m1",
        ],
        env=env,
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert mismatch.returncode != 0, mismatch.stdout
    assert json.loads(mismatch_report.read_text(encoding="utf-8"))["ok"] is False
