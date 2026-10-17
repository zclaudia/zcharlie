#!/usr/bin/env python3
"""Run an isolated one-shot HK/SEC source preflight or licensed probe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from investment_agent.domain.security import SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS
from investment_agent.materials.source_probe import parse_period_range, run_source_probe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight a checked HK/SEC source without network by default. "
            "--allow-network performs one serial, policy-gated local probe."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=(SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS),
        help="one checked source id",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        type=Path,
        help="new, not-yet-existing directory; all output is confined here",
    )
    parser.add_argument(
        "--user-agent",
        default=None,
        help="product/organization identity and valid contact email (required for network; preflight may omit)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="permit one isolated run only when the fixed source policy licenses every identity",
    )
    parser.add_argument(
        "--period",
        action="append",
        default=None,
        metavar="START/END",
        help=(
            "explicit HK annual span as YYYY-MM-DD/YYYY-MM-DD; repeat four consecutive complete years. "
            "SEC selects its own current adapter window and rejects this option."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_source_probe(
            source=args.source,
            work_dir=args.work_dir,
            user_agent=args.user_agent,
            allow_network=args.allow_network,
            periods=(tuple(parse_period_range(item) for item in args.period) if args.period else None),
        )
    except (OSError, ValueError) as exc:
        print(f"source probe refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "dry_run" or report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
