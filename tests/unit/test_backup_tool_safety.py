"""Backup tooling safety: symlinks are never followed, output is private,
and verification refuses manifest paths outside the backup directory."""

import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP = REPO_ROOT / "tools" / "backup_research.py"
VERIFY = REPO_ROOT / "tools" / "verify_backup.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _data_dir_with_symlinks(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    runs = data_dir / "research-runs" / "1"
    runs.mkdir(parents=True)
    sqlite3.connect(data_dir / "app.db").close()
    (runs / "scores.json").write_text("{}")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do-not-copy")
    (runs / "leak.txt").symlink_to(outside)
    (data_dir / "research-runs" / "leakdir").symlink_to(tmp_path)
    return data_dir, outside


def test_backup_skips_symlinks_and_is_private(tmp_path):
    data_dir, _outside = _data_dir_with_symlinks(tmp_path)
    output = tmp_path / "backup"
    result = _run(BACKUP, "--data-dir", str(data_dir), "--output", str(output))
    assert result.returncode == 0, result.stderr

    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert (output / "research-runs" / "1" / "scores.json").is_file()
    assert not (output / "research-runs" / "1" / "leak.txt").exists()
    assert not (output / "research-runs" / "leakdir").exists()
    manifest = json.loads((output / "manifest.json").read_text())
    paths = {entry["path"] for entry in manifest["files"]}
    assert "research-runs/1/scores.json" in paths
    assert not any("leak" in path for path in paths)
    assert all("do-not-copy" not in p.read_text(errors="ignore")
               for p in output.rglob("*") if p.is_file())

    verified = _run(VERIFY, "--backup-dir", str(output))
    assert verified.returncode == 0, verified.stderr


def test_backup_reads_database_outside_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "research-runs" / "1").mkdir(parents=True)
    (data_dir / "research-runs" / "1" / "scores.json").write_text("{}")
    db = tmp_path / "volume" / "app.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("create table marker (v text)")
        conn.execute("insert into marker values ('from-volume')")
    output = tmp_path / "backup"

    result = _run(
        BACKUP, "--data-dir", str(data_dir), "--db", str(db), "--output", str(output)
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(output / "app.db") as conn:
        assert conn.execute("select v from marker").fetchone() == ("from-volume",)
    assert (output / "research-runs" / "1" / "scores.json").is_file()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_db"] == str(db.resolve())
    assert _run(VERIFY, "--backup-dir", str(output)).returncode == 0


def test_verify_rejects_paths_outside_backup_dir(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    (backup_dir / "link.txt").symlink_to(secret)
    manifest = {
        "schema": "zcharlie-backup-manifest.v1",
        "files": [
            {"path": str(secret), "size": 1, "sha256": "0"},
            {"path": "../secret.txt", "size": 1, "sha256": "0"},
            {"path": "link.txt", "size": 1, "sha256": "0"},
        ],
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest))
    result = _run(VERIFY, "--backup-dir", str(backup_dir))
    assert result.returncode == 1
    assert result.stderr.count("unsafe manifest path") == 3
    assert "sha256 mismatch" not in result.stderr


def test_backup_refuses_nonempty_or_overlapping_output_and_missing_source(tmp_path):
    data_dir, _ = _data_dir_with_symlinks(tmp_path)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep")
    result = _run(BACKUP, "--data-dir", str(data_dir), "--output", str(occupied))
    assert result.returncode == 2
    assert (occupied / "keep.txt").read_text() == "keep"

    result = _run(BACKUP, "--data-dir", str(data_dir), "--output", str(data_dir / "nested"))
    assert result.returncode == 2
    assert not (data_dir / "nested").exists()

    absent_data = tmp_path / "absent"
    result = _run(BACKUP, "--data-dir", str(absent_data), "--output", str(tmp_path / "missing-db-backup"))
    assert result.returncode == 2
    assert not (absent_data / "app.db").exists()


def test_verify_rejects_empty_duplicate_and_uncovered_manifests(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    with sqlite3.connect(backup / "app.db") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
    entry = {
        "path": "app.db",
        "size": (backup / "app.db").stat().st_size,
        "sha256": hashlib.sha256((backup / "app.db").read_bytes()).hexdigest(),
    }
    (backup / "extra.txt").write_text("unlisted")
    manifest = {"schema": "zcharlie-backup-manifest.v1", "files": [entry, entry]}
    (backup / "manifest.json").write_text(json.dumps(manifest))
    result = _run(VERIFY, "--backup-dir", str(backup))
    assert result.returncode == 1
    assert "duplicate manifest path: app.db" in result.stderr
    assert "file not covered by manifest: extra.txt" in result.stderr


def test_verify_rejects_noncanonical_alias_paths(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    with sqlite3.connect(backup / "app.db") as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
    entry = {
        "path": "app.db",
        "size": (backup / "app.db").stat().st_size,
        "sha256": hashlib.sha256((backup / "app.db").read_bytes()).hexdigest(),
    }
    alias = {**entry, "path": "./app.db"}
    (backup / "manifest.json").write_text(json.dumps({
        "schema": "zcharlie-backup-manifest.v1", "files": [entry, alias]
    }))
    result = _run(VERIFY, "--backup-dir", str(backup))
    assert result.returncode == 1
    assert "unsafe manifest path: ./app.db" in result.stderr
