"""Verify a backup directory against its manifest.json.

Usage::

    .venv/bin/python tools/verify_backup.py --backup-dir ~/backups/2026-10-06

Recomputes size and SHA-256 for every manifest entry and fails (exit 1)
on any mismatch or missing file. Exits 2 on a missing/invalid manifest.
Manifest paths that are absolute, escape the backup directory, or point at
symbolic links are rejected rather than read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_entry(backup_dir: Path, relative: str) -> Path | None:
    """Path for a manifest entry, or None when it is unsafe to read."""

    if not relative or Path(relative).is_absolute():
        return None
    candidate = backup_dir / relative
    if candidate.is_symlink():
        return None
    try:
        candidate.resolve().relative_to(backup_dir.resolve())
    except ValueError:
        return None
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.backup_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"manifest is not valid JSON: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    seen: set[str] = set()
    for entry in manifest.get("files", []):
        relative = str(entry.get("path", ""))
        seen.add(relative)
        path = resolve_entry(args.backup_dir, relative)
        if path is None:
            failures.append(f"unsafe manifest path: {relative}")
            continue
        if not path.is_file():
            failures.append(f"missing file: {relative}")
            continue
        size = path.stat().st_size
        if size != entry.get("size"):
            failures.append(f"size mismatch: {relative} ({size} != {entry.get('size')})")
        digest = sha256_of(path)
        if digest != entry.get("sha256"):
            failures.append(f"sha256 mismatch: {relative}")

    extra = [
        str(path.relative_to(args.backup_dir))
        for path in sorted(args.backup_dir.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.name != "manifest.json"
        and str(path.relative_to(args.backup_dir)) not in seen
    ]
    for relative in extra:
        failures.append(f"file not covered by manifest: {relative}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print(f"verified {len(seen)} files: all sizes and SHA-256 match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
