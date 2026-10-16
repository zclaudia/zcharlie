"""Synthetic fixtures for P2 contract and capacity tests; no service imports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).parents[1] / "fixtures" / "supplemental_rules_v1" / "expected.json"


def expected_cases() -> dict[str, dict[str, Any]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {case["id"]: case["expected"] for case in document["cases"]}


def synthetic_manifest_stress_shape(*, pair_count: int = 2000) -> dict[str, Any]:
    """Return a deterministic stress shape; this is not a valid/complete P2 DTO.

    The padding deliberately exercises UTF-8 serialization size. It is not an
    entropy model and does not establish the largest legal manifest.
    """

    if not 0 <= pair_count <= 2000:
        raise ValueError("pair_count must be in 0..2000")
    return {
        "schema_version": 1,
        "owner_id": "local_owner",
        "as_of": "2026-10-16T00:00:00+00:00",
        "evaluated_at": "2026-10-16T00:00:01+00:00",
        "rules_generation": 1,
        "events_generation": 1,
        "listing_refs": [{"kind": "listing", "id": i + 1, "revision": 1} for i in range(min(100, pair_count))],
        "contexts": ["company_research"],
        "pairs": [
            {
                "ordinal": i,
                "listing_ref": {"kind": "listing", "id": (i % 100) + 1, "revision": 1},
                "context": "company_research",
                "rule_ref": {"kind": "supplemental_rule", "id": f"{i:08x}-0000-4000-8000-000000000000", "revision": 1},
                "subject": {"ticker": f"T{i:04d}", "company_id": i + 1, "selector_inputs": {"source": "synthetic", "captured_at": "2026-10-16T00:00:00+00:00"}, "factor_values": {}, "evidence": [], "source_manifest": []},
                "match_status": "unknown",
                "input_hash": f"{i:064x}",
                "matching_manifest_hash": f"{i:064x}",
                "padding": "x" * 1500,
            }
            for i in range(pair_count)
        ],
        "scan_diagnostics": ["synthetic upper bound"],
    }
