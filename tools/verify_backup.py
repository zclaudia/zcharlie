"""Verify a backup directory against its manifest.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

CHUNK = 1 << 20


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_entry(backup_dir: Path, relative: str) -> Path | None:
    """Path for a manifest entry, rejecting traversal and every symlink parent."""

    if not relative or "\\" in relative:
        return None
    rel = PurePosixPath(relative)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        return None
    if rel.as_posix() != relative:
        return None
    root = backup_dir.resolve()
    candidate = root
    for part in rel.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return None
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        return None
    return candidate


def _load_manifest(backup_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    absolute = backup_dir.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        return None, [f"backup directory path contains a symbolic link: {backup_dir}"]
    path = backup_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        return None, [f"manifest not found: {path}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"manifest is not valid JSON: {exc}"]
    if not isinstance(value, dict):
        return None, ["manifest must be a JSON object"]
    if value.get("schema") != "zcharlie-backup-manifest.v1":
        return None, ["unsupported or missing manifest schema"]
    files = value.get("files")
    if not isinstance(files, list) or not files:
        return None, ["manifest files must be a non-empty array"]
    return value, []


def _walk_files(root: Path) -> tuple[set[str], list[str]]:
    found: set[str] = set()
    failures: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return found, [f"backup directory is missing or unsafe: {root}"]
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            failures.append(f"symbolic link in backup: {relative}")
        elif path.is_file() and relative != "manifest.json":
            found.add(relative)
    return found, failures


def verify_backup(backup_dir: Path) -> list[str]:
    """Return all manifest and backup integrity failures (empty means valid)."""

    backup_dir = Path(backup_dir)
    manifest, failures = _load_manifest(backup_dir)
    if manifest is None:
        return failures

    seen: set[str] = set()
    entries = manifest["files"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            failures.append(f"invalid manifest entry at index {index}")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative:
            failures.append(f"invalid manifest path at index {index}")
            continue
        if relative in seen:
            failures.append(f"duplicate manifest path: {relative}")
            continue
        seen.add(relative)
        path = resolve_entry(backup_dir, relative)
        if path is None:
            failures.append(f"unsafe manifest path: {relative}")
            continue
        size_expected = entry.get("size")
        digest_expected = entry.get("sha256")
        if not isinstance(size_expected, int) or isinstance(size_expected, bool) or size_expected < 0:
            failures.append(f"invalid size in manifest: {relative}")
            continue
        if not isinstance(digest_expected, str) or len(digest_expected) != 64:
            failures.append(f"invalid sha256 in manifest: {relative}")
            continue
        if not path.is_file():
            failures.append(f"missing file: {relative}")
            continue
        size = path.stat().st_size
        if size != size_expected:
            failures.append(f"size mismatch: {relative} ({size} != {size_expected})")
        if sha256_of(path) != digest_expected:
            failures.append(f"sha256 mismatch: {relative}")

    if "app.db" not in seen:
        failures.append("manifest does not cover required file: app.db")
    found, tree_failures = _walk_files(backup_dir)
    failures.extend(tree_failures)
    for relative in sorted(found - seen):
        failures.append(f"file not covered by manifest: {relative}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()

    failures = verify_backup(args.backup_dir)
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 2 if any(
            failure.startswith(("manifest not found:", "manifest is not valid JSON:",
                                "manifest must be", "unsupported or missing manifest",
                                "manifest files must"))
            for failure in failures
        ) else 1
    manifest, _ = _load_manifest(args.backup_dir)
    assert manifest is not None
    print(f"verified {len(manifest['files'])} files: all sizes and SHA-256 match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
