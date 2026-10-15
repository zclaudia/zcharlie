"""Create a consistent backup of the research database and artifacts.

Usage::

    .venv/bin/python tools/backup_research.py --data-dir ./data --output ~/backups/2026-10-06

``--db`` points at a database kept outside ``DATA_DIR`` (e.g. the Docker
named volume ``/app/db/app.db``); it defaults to ``<data-dir>/app.db``.

Produces ``<output>/app.db`` (consistent SQLite backup), copies
``research-runs/``, ``notifications/``, ``research-material/``,
``reviewed-annual/`` and ``calendars/`` next to it, and writes a
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
    # mode=ro prevents sqlite3 from silently creating a missing source file.
    source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
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


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _make_private(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
        elif path.is_file() and not path.is_symlink():
            path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("./data"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.output.is_symlink():
        print(f"output must not be a symbolic link: {args.output}", file=sys.stderr)
        return 2
    data_dir = args.data_dir.resolve()
    output = args.output.resolve()
    db_source = args.db.resolve() if args.db is not None else data_dir / "app.db"
    if not db_source.is_file():
        print(f"database not found: {db_source}", file=sys.stderr)
        return 2

    if _overlaps(output, data_dir) or _overlaps(output, db_source):
        print("output must not overlap the data directory or source database", file=sys.stderr)
        return 2
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        print(f"output must be a new or empty directory: {output}", file=sys.stderr)
        return 2

    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    try:
        backup_sqlite(db_source, output / "app.db")

        entries: list[dict[str, object]] = []
        for relative in (
            "research-runs", "notifications", "research-material", "reviewed-annual",
            "calendars",
        ):
            source_dir = data_dir / relative
            if source_dir.is_symlink() or not source_dir.is_dir():
                continue
            shutil.copytree(
                source_dir,
                output / relative,
                symlinks=True,
                ignore=_ignore_symlinks,
            )

        _make_private(output)
        for path in sorted(output.rglob("*")):
            if path.is_file() and not path.is_symlink():
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
            "source_db": str(db_source),
            "files": entries,
        }
        manifest_path = output / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest_path.chmod(0o600)
    except Exception as exc:
        shutil.rmtree(output, ignore_errors=True)
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"backup written to {output} ({len(entries)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
