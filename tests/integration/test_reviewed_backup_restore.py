"""Reviewed annual originals and runtime calendars survive backup relocation."""

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _source_data(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    data = tmp_path / "source-data"
    annual = data / "reviewed-annual" / "sources" / "hk207"
    annual.mkdir(parents=True)
    originals = {
        "evidence-index.json": b'{"company_key":"hk207","review":"accepted"}\n',
        "annual-report.pdf": b"immutable reviewed original bytes\x00\x01",
    }
    for name, content in originals.items():
        (annual / name).write_bytes(content)
    (data / "calendars").mkdir()
    (data / "calendars" / "a_share.yaml").write_text("version: runtime-reviewed\n")
    with sqlite3.connect(data / "app.db") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('source')")
    return data, originals


def test_reviewed_originals_calendar_and_sqlite_restore_after_relocation(tmp_path):
    data, originals = _source_data(tmp_path)
    backup = tmp_path / "backup"
    created = _run("backup_research.py", "--data-dir", str(data), "--output", str(backup))
    assert created.returncode == 0, created.stderr
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    files = {item["path"] for item in json.loads((backup / "manifest.json").read_text())["files"]}
    assert "calendars/a_share.yaml" in files
    assert "reviewed-annual/sources/hk207/annual-report.pdf" in files
    for name, content in originals.items():
        copied = backup / "reviewed-annual" / "sources" / "hk207" / name
        assert copied.read_bytes() == content
        assert hashlib.sha256(copied.read_bytes()).hexdigest()
    assert _run("verify_backup.py", "--backup-dir", str(backup)).returncode == 0

    restored = tmp_path / "relocated" / "data"
    result = _run("restore_research.py", "--backup-dir", str(backup), "--output", str(restored))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700
    assert (restored / "calendars" / "a_share.yaml").read_text() == "version: runtime-reviewed\n"
    for name, content in originals.items():
        path = restored / "reviewed-annual" / "sources" / "hk207" / name
        assert path.read_bytes() == content
    with sqlite3.connect(restored / "app.db") as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("source",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_restore_rejects_damage_unsafe_manifest_and_existing_output(tmp_path):
    data, _ = _source_data(tmp_path)
    backup = tmp_path / "backup"
    assert _run("backup_research.py", "--data-dir", str(data), "--output", str(backup)).returncode == 0

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("safe")
    refused = _run("restore_research.py", "--backup-dir", str(backup), "--output", str(existing))
    assert refused.returncode == 1
    assert (existing / "keep").read_text() == "safe"

    damaged = backup / "reviewed-annual" / "sources" / "hk207" / "annual-report.pdf"
    damaged.write_bytes(b"changed")
    target = tmp_path / "must-not-remain"
    refused = _run("restore_research.py", "--backup-dir", str(backup), "--output", str(target))
    assert refused.returncode == 1
    assert not target.exists()


def test_restore_removes_staging_when_valid_manifest_contains_corrupt_sqlite(tmp_path):
    data, _ = _source_data(tmp_path)
    backup = tmp_path / "backup"
    assert _run("backup_research.py", "--data-dir", str(data), "--output", str(backup)).returncode == 0

    db_copy = backup / "app.db"
    corrupted = bytearray(db_copy.read_bytes())
    corrupted[:16] = b"not a sqlite db!"
    db_copy.write_bytes(corrupted)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    db_entry = next(item for item in manifest["files"] if item["path"] == "app.db")
    db_entry["size"] = db_copy.stat().st_size
    db_entry["sha256"] = hashlib.sha256(corrupted).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    target = tmp_path / "restore-target"
    refused = _run("restore_research.py", "--backup-dir", str(backup), "--output", str(target))
    assert refused.returncode == 1
    assert "SQLite integrity check failed" in refused.stderr
    assert not target.exists()
    assert not list(tmp_path.glob(".restore-target.restore-*"))

    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["files"].append({"path": "../../outside", "size": 0, "sha256": "0" * 64})
    (backup / "manifest.json").write_text(json.dumps(manifest))
    refused = _run("restore_research.py", "--backup-dir", str(backup), "--output", str(target))
    assert refused.returncode == 1
    assert not target.exists()
