"""Material originals and nested manifests survive backup and relocation."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_backup_includes_material_bodies_and_nested_manifests(tmp_path: Path) -> None:
    data = tmp_path / "data"
    material = data / "research-material" / "archive" / "example"
    material.mkdir(parents=True)
    with sqlite3.connect(data / "app.db") as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO evidence VALUES (1)")
    (material / "manifest.json").write_text('{"schema_version":1}', encoding="utf-8")
    (material / "body.bin").write_bytes(b"exact original response")
    backup = tmp_path / "backup"
    result = subprocess.run(
        [sys.executable, str(REPO / "tools/backup_research.py"),
         "--data-dir", str(data), "--output", str(backup)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    files = {item["path"] for item in json.loads((backup / "manifest.json").read_text())["files"]}
    assert "research-material/archive/example/manifest.json" in files
    assert "research-material/archive/example/body.bin" in files
    verify = [sys.executable, str(REPO / "tools/verify_backup.py"), "--backup-dir", str(backup)]
    assert subprocess.run(verify, capture_output=True, check=False).returncode == 0
    restored = tmp_path / "restored"
    shutil.copytree(backup, restored)
    shutil.rmtree(data)
    shutil.rmtree(backup)
    assert (restored / "research-material/archive/example/body.bin").read_bytes() == b"exact original response"
    verify[-1] = str(restored)
    assert subprocess.run(verify, capture_output=True, check=False).returncode == 0
    with sqlite3.connect(restored / "app.db") as connection:
        assert connection.execute("SELECT id FROM evidence").fetchall() == [(1,)]
    (restored / "research-material/archive/example/manifest.json").write_text("tampered")
    invalid = subprocess.run(verify, capture_output=True, text=True, check=False)
    assert invalid.returncode == 1
    assert "mismatch" in invalid.stderr
