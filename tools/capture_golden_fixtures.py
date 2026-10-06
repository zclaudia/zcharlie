"""Capture and sanitize golden corpus fixtures through the live ifind-proxy.

Developer tool for work package A2 (docs/product/mvp-completion-plan.md).
For every manifest sample still marked ``pending`` it fetches the raw provider
responses, sanitizes them, regenerates the canonical inputs, freezes the
current deterministic kernel output as the golden expectation, and writes a
per-sample fixture directory under ``tests/fixtures/research_kernel/``.

Usage::

    export IFIND_PROXY_API_KEY=...   # the ifind-proxy PROXY_API_KEY value
    .venv/bin/python tools/capture_golden_fixtures.py                # all pending
    .venv/bin/python tools/capture_golden_fixtures.py --ticker 601398.SH

The tool only talks to ifind-proxy; it never calls the LLM. Credentials are
read from the environment and are never written to disk. Sanitization drops
any key whose name looks credential-bearing and redacts credential-shaped
string values, then re-scans every written file (including against the live
key value) and fails loudly if anything sensitive survives.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from investment_agent.research.ifind_chain import (  # noqa: E402
    IfindProxyClient,
    fetch_canonical_bundle,
)
from investment_agent.research.kernel import InProcessResearchKernel  # noqa: E402
from investment_agent.research_kernel.scoring import SCORING_VERSION  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / "research_kernel" / "manifest.yaml"
FIXTURE_ROOT = MANIFEST_PATH.parent
DEFAULT_AS_OF = date(2026, 10, 5)

SENSITIVE_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "credential",
)
CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{11,}"),
    re.compile(r"(?i)bearer[ \t]+[A-Za-z0-9._~-]{12,}"),
    re.compile(r"(?i)x-api-key[\"'=: \t]+[A-Za-z0-9._~-]{8,}"),
)
REDACTED = "[REDACTED]"

FIXTURE_FILES = (
    "provider_raw.json",
    "pool.json",
    "market.json",
    "financial.json",
    "golden_scores.json",
    "golden_report_context.json",
)


def sanitize(value: Any) -> Any:
    """Recursively drop credential-bearing keys and redact credential values."""

    if isinstance(value, dict):
        return {
            key: sanitize(item)
            for key, item in value.items()
            if not any(pattern in str(key).lower() for pattern in SENSITIVE_KEY_SUBSTRINGS)
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        for pattern in CREDENTIAL_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    return value


def scan_for_credentials(text: str, *, live_key: str) -> list[str]:
    """Return human-readable descriptions of anything credential-shaped."""

    hits: list[str] = []
    for pattern in CREDENTIAL_VALUE_PATTERNS:
        if pattern.search(text):
            hits.append(f"credential-shaped value: {pattern.pattern}")
    if live_key and live_key in text:
        hits.append("live proxy API key appears verbatim")
    return hits


def write_json(path: Path, payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


async def fetch_bundle_with_retry(
    client: IfindProxyClient,
    *,
    sample: dict[str, Any],
    report_year: int,
    as_of: date,
    max_attempts: int = 5,
) -> Any:
    """Fetch one canonical bundle, waiting out proxy rate-limit/server errors."""

    for attempt in range(1, max_attempts + 1):
        try:
            return await fetch_canonical_bundle(
                client,
                ticker=sample["ticker"],
                industry=sample["industry"],
                report_year=report_year,
                as_of=as_of,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRYABLE_STATUS_CODES or attempt == max_attempts:
                raise
            wait_seconds = 20 * attempt
            print(
                f"[{sample['ticker']}] proxy returned {exc.response.status_code}, "
                f"waiting {wait_seconds}s (attempt {attempt}/{max_attempts}) ..."
            )
            await asyncio.sleep(wait_seconds)
    raise AssertionError("unreachable")


async def capture_one(
    client: IfindProxyClient,
    *,
    sample: dict[str, Any],
    report_year: int,
    as_of: date,
    live_key: str,
) -> Path:
    ticker = sample["ticker"]
    print(f"[{ticker}] fetching via ifind-proxy ...")
    bundle = await fetch_bundle_with_retry(
        client,
        sample=sample,
        report_year=report_year,
        as_of=as_of,
    )
    retrieved_at = str(bundle.raw.get("retrieved_at", datetime.now().isoformat()))

    raw = sanitize(bundle.raw)
    pool = sanitize(bundle.pool)
    market = sanitize(bundle.market)
    financial = sanitize(bundle.financial)

    fixture_dir = FIXTURE_ROOT / ticker.split(".")[0]
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True)

    written: dict[str, str] = {
        "provider_raw.json": write_json(fixture_dir / "provider_raw.json", raw),
        "pool.json": write_json(fixture_dir / "pool.json", pool),
        "market.json": write_json(fixture_dir / "market.json", market),
        "financial.json": write_json(fixture_dir / "financial.json", financial),
    }

    print(f"[{ticker}] scoring with deterministic kernel ...")
    kernel = InProcessResearchKernel()
    artifacts = kernel.score_and_render(
        pool=fixture_dir / "pool.json",
        market=fixture_dir / "market.json",
        financial=fixture_dir / "financial.json",
        artifact_dir=fixture_dir,
        as_of=retrieved_at,
    )
    written["golden_scores.json"] = write_json(
        fixture_dir / "golden_scores.json",
        json.loads(artifacts.scores_json.read_text(encoding="utf-8")),
    )
    written["golden_report_context.json"] = write_json(
        fixture_dir / "golden_report_context.json",
        json.loads(artifacts.report_context_json.read_text(encoding="utf-8")),
    )
    for temp_artifact in (
        artifacts.scores_json,
        artifacts.scores_csv,
        artifacts.report_context_json,
        artifacts.report_markdown,
    ):
        temp_artifact.unlink(missing_ok=True)

    problems: list[str] = []
    for name, text in written.items():
        for hit in scan_for_credentials(text, live_key=live_key):
            problems.append(f"{name}: {hit}")
    if problems:
        for problem in problems:
            print(f"[{ticker}] CREDENTIAL RISK {problem}", file=sys.stderr)
        raise SystemExit(
            f"sanitized output for {ticker} still looks sensitive; "
            "improve the sanitizer before committing fixtures"
        )

    readme = f"""# Golden case: {ticker} ({sample['name']})

Captured from the live ifind-proxy on {as_of.isoformat()} (retrieved_at {retrieved_at}).

- provider_raw.json: sanitized raw proxy responses (ifind-raw.v1)
- pool.json / market.json / financial.json: canonical inputs (ifind-canonical.v1)
- golden_scores.json / golden_report_context.json: deterministic kernel
  output frozen as the offline golden expectation (scoring {SCORING_VERSION})

Sanitization: credential-bearing keys dropped, credential-shaped values
redacted; the full written payload was re-scanned against credential
patterns and the live key before this directory was accepted. Declared
sample traits: {', '.join(sample['traits'])}.
"""
    (fixture_dir / "README.md").write_text(readme, encoding="utf-8")
    print(f"[{ticker}] wrote {fixture_dir.relative_to(REPO_ROOT)}")
    return fixture_dir


async def run_capture(
    args: argparse.Namespace,
    samples: list[dict[str, Any]],
    live_key: str,
    proxy_url: str,
    report_year: int,
) -> int:
    as_of = date.fromisoformat(args.as_of)
    captured = 0
    for index, sample in enumerate(samples):
        if index:
            await asyncio.sleep(args.pause_seconds)
        async with IfindProxyClient(base_url=proxy_url, api_key=live_key) as client:
            await capture_one(
                client,
                sample=sample,
                report_year=report_year,
                as_of=as_of,
                live_key=live_key,
            )
        captured += 1
    return captured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", help="limit capture to these tickers")
    parser.add_argument(
        "--as-of",
        default=DEFAULT_AS_OF.isoformat(),
        help="research as-of date (default: 2026-10-05)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recapture even if the sample is already marked sanitized",
    )
    parser.add_argument(
        "--pause-seconds",
        type=int,
        default=15,
        help="seconds to sleep between samples to stay under the proxy rate limit",
    )
    args = parser.parse_args()

    live_key = os.environ.get("IFIND_PROXY_API_KEY", "").strip()
    if not live_key:
        print(
            "IFIND_PROXY_API_KEY is required (the ifind-proxy PROXY_API_KEY value)",
            file=sys.stderr,
        )
        return 2
    proxy_url = os.environ.get("IFIND_PROXY_URL", "http://127.0.0.1:7100").strip()

    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    report_year = int(manifest["report_year"])
    selected = set(args.ticker or [])
    samples = []
    for sample in manifest["samples"]:
        if selected and sample["ticker"] not in selected:
            continue
        already_captured = (FIXTURE_ROOT / sample["ticker"].split(".")[0]).is_dir()
        if already_captured and not args.force:
            print(f"[{sample['ticker']}] fixture directory exists, skipping (use --force to recapture)")
            continue
        samples.append(sample)
    if not samples:
        print("nothing to capture (no pending samples matched)")
        return 0

    captured = asyncio.run(
        run_capture(args, samples, live_key, proxy_url, report_year)
    )
    print(f"captured {captured} sample(s); now update manifest fixture_status/collected_at")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
