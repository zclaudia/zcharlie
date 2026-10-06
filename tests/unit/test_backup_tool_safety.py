"""Backup tooling safety: symlinks are never followed, output is private,
and verification refuses manifest paths outside the backup directory."""

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
