#!/usr/bin/env python3
"""Write an offline release audit report for a restored HK2-08 package."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from investment_agent.materials.release_audit import audit_release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--require-m1", action="store_true")
    return parser


def _report_target(report: Path, database: Path, data_dir: Path) -> Path:
    target = report.expanduser().absolute()
    if target.exists() and target.is_symlink():
        raise ValueError("report path cannot be a symbolic link")
    parent = target.parent
    if not parent.is_dir():
        raise ValueError("report parent directory must already exist")
    resolved_parent = parent.resolve(strict=True)
    resolved = resolved_parent / target.name
    database_real = database.expanduser().absolute().resolve(strict=False)
    data_real = data_dir.expanduser().absolute().resolve(strict=False)
    protected = {database_real}
    protected.update(Path(str(database_real) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    if resolved in protected:
        raise ValueError("report path cannot overwrite the database or a SQLite sidecar")
    if resolved.exists():
        for path in protected:
            if path.exists() and os.path.samefile(resolved, path):
                raise ValueError("report path cannot overwrite the database or a SQLite sidecar")
    if resolved.is_relative_to(data_real):
        raise ValueError("report path must be outside data_dir so it cannot overwrite an original")
    if target.exists() and target.resolve(strict=True).is_relative_to(data_real):
        raise ValueError("report path resolves inside data_dir")
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report_path = _report_target(args.report, args.database, args.data_dir)
        result = audit_release(args.database, args.data_dir, require_m1=args.require_m1)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"release audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": result["ok"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
