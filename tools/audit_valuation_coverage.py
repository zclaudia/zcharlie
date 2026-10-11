"""Read-only AG03 entry: explicit --database prevents silent fallback to another DB."""
import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from investment_agent.investor.coverage import audit_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--universe", default="CSI300")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-pool-version")
    args = parser.parse_args()
    report = asyncio.run(audit_database(args.database, universe=args.universe, as_of=args.as_of))
    if args.expected_pool_version and args.expected_pool_version != report["target_pool_version"]:
        parser.error("target pool changed: refusing to overwrite report")
    args.output.write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
