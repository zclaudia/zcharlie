"""Restore a verified backup into a new, private output directory.

Usage::

    .venv/bin/python tools/restore_research.py --backup-dir PATH --output PATH
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from verify_backup import verify_backup


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _sqlite_integrity(path: Path) -> str | None:
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        return f"SQLite integrity check failed: {exc}"
    if result is None or result[0] != "ok":
        return f"SQLite integrity check failed: {result[0] if result else 'no result'}"
    return None


def restore(backup_dir: Path, output: Path) -> list[str]:
    if output.is_symlink():
        return [f"output must not be a symbolic link: {output}"]
    backup_dir = Path(backup_dir)
    failures = verify_backup(backup_dir)
    if failures:
        return [f"backup verification failed: {failure}" for failure in failures]
    backup_dir = backup_dir.resolve()
    output = output.resolve()
    if _overlaps(backup_dir, output):
        return ["output must not overlap the backup directory"]
    if os.path.lexists(output):
        return [f"output must not already exist: {output}"]

    staging: Path | None = None
    try:
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.restore-", dir=output.parent)
        )
        staging.chmod(0o700)
        for source in sorted(backup_dir.rglob("*")):
            if source.is_dir():
                continue
            relative = source.relative_to(backup_dir)
            target = staging / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o600)
        for directory in sorted((p for p in staging.rglob("*") if p.is_dir()), reverse=True):
            directory.chmod(0o700)
        staging.chmod(0o700)

        copied_failures = verify_backup(staging)
        if copied_failures:
            return [f"restored copy verification failed: {failure}" for failure in copied_failures]
        sqlite_failure = _sqlite_integrity(staging / "app.db")
        if sqlite_failure:
            return [sqlite_failure]
        if os.path.lexists(output):
            return [f"output appeared during restore; refusing to replace it: {output}"]
        os.rename(staging, output)
        staging = None
        return []
    except Exception as exc:
        return [f"restore failed: {exc}"]
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    failures = restore(args.backup_dir, args.output)
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print(f"restored verified backup to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
