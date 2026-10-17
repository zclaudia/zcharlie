"""Offline HK2-06 source-observation evidence auditor.

Example::

    PYTHONPATH=src python tools/hk206_source_acceptance.py \
        --ledger /path/to/ledger.json --evidence-root /path/to/evidence

The command reads only those explicit inputs and local content-addressed
archives. It does not load dotenv/settings, access a database, or use network.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from investment_agent.materials.source_observations import (
    ObservationAuditError,
    audit_observations,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="explicit JSON ledger")
    parser.add_argument(
        "--evidence-root", type=Path, required=True,
        help="directory containing report paths, archive refs and review citations",
    )
    parser.add_argument(
        "--output", type=Path,
        help="optional new JSON output file; existing files and evidence-root paths are refused",
    )
    args = parser.parse_args(argv)
    try:
        result = audit_observations(args.ledger, args.evidence_root)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            _write_new_output(args.output, args.ledger, args.evidence_root, encoded)
        return 0
    except (ObservationAuditError, OSError, ValueError) as exc:
        parser.exit(2, f"hk206 source acceptance: {exc}\n")


def _write_new_output(output: Path, ledger: Path, evidence_root: Path, content: str) -> None:
    cursor = output.absolute()
    for path_part in (cursor, *cursor.parents):
        if path_part.is_symlink():
            raise ObservationAuditError("output path must not traverse a symlink")
    resolved_parent = output.parent.resolve(strict=True)
    destination = resolved_parent / output.name
    if destination.exists():
        raise ObservationAuditError("output path already exists; refusing to overwrite")
    ledger_path = ledger.resolve(strict=True)
    root_path = evidence_root.resolve(strict=True)
    if destination == ledger_path or root_path == destination or root_path in destination.parents:
        raise ObservationAuditError("output must be outside evidence inputs")
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(content)


if __name__ == "__main__":
    raise SystemExit(main())
