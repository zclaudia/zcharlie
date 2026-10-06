"""Manifest-driven golden parity tests for the deterministic research kernel.

Every sanitized corpus sample in ``tests/fixtures/research_kernel/manifest.yaml``
must replay offline, in any checkout, with byte-semantically identical kernel
output:

- Every sample is compared against its own frozen golden output
  (``golden_scores.json`` / ``golden_report_context.json``) for the current
  scoring version (regenerated offline by
  ``tools/regenerate_golden_fixtures.py`` on a version bump).
- 600406.SH additionally keeps the read-only ``my-stocks`` 0.3.0 legacy
  oracle (``legacy_scores.json`` / ``legacy_report_context.json``) as the
  historical scoring v1.2 parity record; it is only used when no golden
  output exists.

Comparison rules (work package A3): classification, labels, missing items,
quality flags and versions must match exactly; numeric fields compare with an
explicit tolerance; the wall-clock ``generated_at`` metadata field is
normalized away. Mismatch failures list JSON pointers with expected and
actual values. Golden files are never updated to make a failing test pass —
algorithm changes require a scoring version bump plus a written diff
explanation (docs/product/mvp-completion-plan.md, execution discipline).
"""

import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from investment_agent.research.kernel import InProcessResearchKernel

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.yaml"

FLOAT_TOLERANCE = 1e-9
NORMALIZED_METADATA_FIELDS = ("generated_at",)


def _manifest_samples() -> list[dict[str, Any]]:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    samples = [sample for sample in manifest["samples"] if isinstance(sample, dict)]
    assert samples, "golden corpus manifest must list samples"
    return samples


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _drop_normalized_fields(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        payload = {**payload, "metadata": {k: v for k, v in metadata.items() if k not in NORMALIZED_METADATA_FIELDS}}
    return payload


def _collect_diffs(
    expected: Any,
    actual: Any,
    *,
    pointer: str = "$",
    diffs: list[str] | None = None,
) -> list[str]:
    if diffs is None:
        diffs = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                diffs.append(f"{pointer}/{key}: unexpected, expected=absent actual={actual[key]!r}")
            elif key not in actual:
                diffs.append(f"{pointer}/{key}: missing, expected={expected[key]!r} actual=absent")
            else:
                _collect_diffs(expected[key], actual[key], pointer=f"{pointer}/{key}", diffs=diffs)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append(f"{pointer}: length, expected={len(expected)} actual={len(actual)}")
        else:
            for index, (exp_item, act_item) in enumerate(zip(expected, actual, strict=True)):
                _collect_diffs(exp_item, act_item, pointer=f"{pointer}/{index}", diffs=diffs)
    elif isinstance(expected, bool) or isinstance(actual, bool):
        if expected is not actual:
            diffs.append(f"{pointer}: expected={expected!r} actual={actual!r}")
    elif isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not math.isclose(
            float(expected), float(actual), rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE
        ):
            diffs.append(f"{pointer}: expected={expected!r} actual={actual!r}")
    elif expected != actual:
        diffs.append(f"{pointer}: expected={expected!r} actual={actual!r}")
    return diffs


def _golden_files(fixture_dir: Path) -> tuple[Path, Path]:
    golden_scores = fixture_dir / "golden_scores.json"
    if golden_scores.is_file():
        return golden_scores, fixture_dir / "golden_report_context.json"
    return fixture_dir / "legacy_scores.json", fixture_dir / "legacy_report_context.json"


@pytest.mark.parametrize("sample", _manifest_samples(), ids=lambda sample: sample["ticker"])
def test_kernel_replay_matches_frozen_golden_output(tmp_path: Path, sample: dict[str, Any]):
    assert sample["fixture_status"] == "sanitized", (
        f"{sample['ticker']} is not captured yet; work package A2 must sanitize it first"
    )
    fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
    scores_path, context_path = _golden_files(fixture_dir)
    expected_scores = _drop_normalized_fields(_read_json(scores_path))
    expected_context = _read_json(context_path)
    as_of = _read_json(scores_path)["metadata"]["freshness_policy"]["as_of"]

    artifacts = InProcessResearchKernel().score_and_render(
        pool=fixture_dir / "pool.json",
        market=fixture_dir / "market.json",
        financial=fixture_dir / "financial.json",
        artifact_dir=tmp_path,
        as_of=as_of,
    )

    actual_scores = _drop_normalized_fields(_read_json(artifacts.scores_json))
    actual_context = _read_json(artifacts.report_context_json)

    diffs = _collect_diffs(expected_scores, actual_scores, pointer=f"$({sample['ticker']}).scores")
    diffs += _collect_diffs(
        expected_context.get("items"), actual_context.get("items"), pointer=f"$({sample['ticker']}).context.items"
    )
    assert not diffs, (
        f"golden replay drifted for {sample['ticker']} "
        f"(scoring version changes need a version bump and a written explanation):\n"
        + "\n".join(diffs[:20])
    )

    assert artifacts.scores_csv.read_text(encoding="utf-8").startswith("symbol,name,strategy_label")
    assert artifacts.report_markdown.read_text(encoding="utf-8").startswith("# 股票研究上下文")


def test_golden_corpus_files_exist_for_every_sanitized_sample():
    for sample in _manifest_samples():
        fixture_dir = FIXTURE_ROOT / sample["ticker"].split(".")[0]
        legacy_sample = (fixture_dir / "legacy_scores.json").is_file()
        if not legacy_sample:
            # 600406.SH was captured before raw provider responses were retained;
            # its parity contract is the canonical inputs plus the legacy oracle.
            assert (fixture_dir / "provider_raw.json").is_file(), (
                f"{sample['ticker']} missing sanitized fixture provider_raw.json"
            )
        for filename in ("pool.json", "market.json", "financial.json"):
            assert (fixture_dir / filename).is_file(), (
                f"{sample['ticker']} missing sanitized fixture {filename}"
            )
        scores_path, context_path = _golden_files(fixture_dir)
        assert scores_path.is_file() and context_path.is_file(), (
            f"{sample['ticker']} missing frozen golden kernel output"
        )
