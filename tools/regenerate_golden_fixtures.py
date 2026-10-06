"""Offline regeneration of golden corpus expectations after a scoring version bump.

Developer tool for the execution discipline in
docs/product/engineering-baseline.md: an algorithm change must bump the
scoring version and ship an old-vs-new regression report. This tool never
touches the network. For every sanitized manifest sample it:

1. replays ``provider_raw.json`` (when present) through
   ``canonicalize_ifind_results`` with the sample's own retrieval timestamp
   and ``collected_at`` as-of date, rewriting pool/market/financial.json;
2. rescores the canonical inputs with the current kernel at the as-of
   timestamp frozen in the previous golden output;
3. writes ``golden_scores.json`` / ``golden_report_context.json`` (samples
   with a read-only ``legacy_*`` oracle keep it untouched and gain golden
   files next to it);
4. prints a Markdown old-vs-new summary per ticker (``--report`` also writes
   it to a file).

Usage::

    .venv/bin/python tools/regenerate_golden_fixtures.py --report /tmp/diff.md
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from investment_agent.research.ifind_chain import (  # noqa: E402
    IfindProxyResult,
    canonicalize_ifind_results,
    parse_proxy_rows,
)
from investment_agent.research.kernel import InProcessResearchKernel  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "research_kernel"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"
SUMMARY_FIELDS = (
    "investment_score",
    "research_confidence_score",
    "valuation_level",
    "suggestion",
    "needs_agent_review",
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _decode_raw(raw: dict[str, Any]) -> dict[str, IfindProxyResult]:
    return {
        name: IfindProxyResult(
            path=str(entry["path"]), body=entry["body"], rows=parse_proxy_rows(entry["body"])
        )
        for name, entry in raw["requests"].items()
    }


def _previous_scores(fixture_dir: Path) -> dict[str, Any]:
    golden = fixture_dir / "golden_scores.json"
    if golden.is_file():
        return _read_json(golden)
    return _read_json(fixture_dir / "legacy_scores.json")


def regenerate_sample(sample: dict[str, Any], report_year: int) -> dict[str, Any]:
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    previous = _previous_scores(fixture_dir)
    as_of = previous["metadata"]["freshness_policy"]["as_of"]
    old_item = previous["items"][0]
    old_market = _read_json(fixture_dir / "market.json")["items"][0]

    raw_path = fixture_dir / "provider_raw.json"
    if raw_path.is_file():
        raw = _read_json(raw_path)
        bundle = canonicalize_ifind_results(
            ticker=sample["ticker"],
            industry=sample["industry"],
            report_year=report_year,
            results=_decode_raw(raw),
            retrieved_at=str(raw["retrieved_at"]),
            as_of=date.fromisoformat(str(sample["collected_at"])),
        )
        _write_json(fixture_dir / "pool.json", bundle.pool)
        _write_json(fixture_dir / "market.json", bundle.market)
        _write_json(fixture_dir / "financial.json", bundle.financial)

    with tempfile.TemporaryDirectory() as tmp:
        artifacts = InProcessResearchKernel().score_and_render(
            pool=fixture_dir / "pool.json",
            market=fixture_dir / "market.json",
            financial=fixture_dir / "financial.json",
            artifact_dir=Path(tmp),
            as_of=as_of,
        )
        new_scores = _read_json(artifacts.scores_json)
        new_context = _read_json(artifacts.report_context_json)
    _write_json(fixture_dir / "golden_scores.json", new_scores)
    _write_json(fixture_dir / "golden_report_context.json", new_context)

    new_item = new_scores["items"][0]
    new_market = _read_json(fixture_dir / "market.json")["items"][0]
    return {
        "ticker": sample["ticker"],
        "name": sample["name"],
        "old": {field: old_item.get(field) for field in SUMMARY_FIELDS},
        "new": {field: new_item.get(field) for field in SUMMARY_FIELDS},
        "old_return_1y": old_market.get("return_1y"),
        "new_return_1y": new_market.get("return_1y"),
        "old_drawdown": old_market.get("max_drawdown_1y"),
        "new_drawdown": new_market.get("max_drawdown_1y"),
        "factor_coverage": new_item.get("factor_coverage"),
        "missing_factors": new_item.get("missing_factors", []),
    }


def render_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Ticker | Name | Score old → new | Confidence old → new | Valuation | "
        "Suggestion old → new | Review old → new | Factor coverage | Missing factors |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        old, new = row["old"], row["new"]
        valuation = (
            old["valuation_level"]
            if old["valuation_level"] == new["valuation_level"]
            else f"{old['valuation_level']} → {new['valuation_level']}"
        )
        lines.append(
            f"| {row['ticker']} | {row['name']} "
            f"| {old['investment_score']} → {new['investment_score']} "
            f"| {old['research_confidence_score']} → {new['research_confidence_score']} "
            f"| {valuation} "
            f"| {old['suggestion']} → {new['suggestion']} "
            f"| {old['needs_agent_review']} → {new['needs_agent_review']} "
            f"| {row['factor_coverage']} "
            f"| {', '.join(row['missing_factors']) or '-'} |"
        )
    lines.extend(
        [
            "",
            "| Ticker | return_1y old → new | max_drawdown_1y old → new |",
            "|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            f"| {row['ticker']} | {row['old_return_1y']} → {row['new_return_1y']} "
            f"| {row['old_drawdown']} → {row['new_drawdown']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", help="limit to these tickers")
    parser.add_argument("--report", type=Path, help="also write the Markdown summary here")
    args = parser.parse_args()

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    report_year = int(manifest["report_year"])
    selected = set(args.ticker or [])
    rows = [
        regenerate_sample(sample, report_year)
        for sample in manifest["samples"]
        if sample["fixture_status"] == "sanitized"
        and (not selected or sample["ticker"] in selected)
    ]
    report = render_report(rows)
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
