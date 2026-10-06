"""Create a consistent backup of the research database and artifacts.

Usage::

    .venv/bin/python tools/backup_research.py --data-dir ./data --output ~/backups/2026-10-06

Produces ``<output>/app.db`` (consistent SQLite backup), copies
``research-runs/`` and ``notifications/`` next to it, and writes a
``manifest.json`` recording every file's size and SHA-256 for offline
verification (see docs/product/backup-and-recovery.md).

The SQLite online-backup API yields a consistent snapshot even while
writers are active, but the runbook asks you to stop API/worker/scheduler
first so the copied artifact directories match the database exactly.

Symbolic links inside the artifact directories are skipped (never followed),
so a stray link cannot pull files from outside ``DATA_DIR`` into the backup.
The output directory is created ``0700`` because it holds the full database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(source)
    try:
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if os.path.islink(os.path.join(directory, name))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    output = args.output.resolve()
    db_source = data_dir / "app.db"
    if not db_source.is_file():
        print(f"database not found: {db_source}", file=sys.stderr)
        return 2

    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    backup_sqlite(db_source, output / "app.db")
    (output / "app.db").chmod(0o600)

    entries: list[dict[str, object]] = []
    for relative in ("research-runs", "notifications"):
        source_dir = data_dir / relative
        if source_dir.is_symlink() or not source_dir.is_dir():
            continue
        target_dir = output / relative
        shutil.copytree(
            source_dir,
            target_dir,
            symlinks=True,
            ignore=_ignore_symlinks,
            dirs_exist_ok=True,
        )

    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file() and path.name != "manifest.json":
            entries.append(
                {
                    "path": str(path.relative_to(output)),
                    "size": path.stat().st_size,
                    "sha256": sha256_of(path),
                }
            )

    manifest = {
        "schema": "zcharlie-backup-manifest.v1",
        "created_at_iso": datetime.now().isoformat(),
        "source_data_dir": str(data_dir),
        "files": entries,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"backup written to {output} ({len(entries)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
