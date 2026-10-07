"""Deviation report: snapshot mode vs iFinD mode on golden samples (P1-B5).

Informational only - no thresholds are asserted (p1-plan B5). The golden
corpus itself stays on the iFinD fixtures (D3, see the golden replay tests);
here the samples that also have captured free-source (baostock) factor
fixtures are run through snapshot mode and compared field by field. The
report is printed (``pytest -s``) and written to the test's tmp directory.
"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from investment_agent.research.snapshot_source import build_snapshot_bundle
from tests.unit._factor_support import FIXTURE_DIR, baostock_view
from tests.unit._snapshot_deviation import (
    COMPARED_FIELDS,
    COMPARED_SCORE_FIELDS,
    FIXTURE_ROOT,
    deviation_rows,
    golden_bundle,
    render_table,
)

MANIFEST = yaml.safe_load((FIXTURE_ROOT / "manifest.yaml").read_text(encoding="utf-8"))
BOTH = [
    sample
    for sample in MANIFEST["samples"]
    if (FIXTURE_DIR / f"baostock_{sample['ticker'].split('.')[0]}.json").is_file()
    and (FIXTURE_ROOT / sample["ticker"].split(".")[0] / "provider_raw.json").is_file()
]


def test_samples_with_both_fixtures_exist():
    assert {sample["ticker"] for sample in BOTH} >= {"600519.SH", "601398.SH"}


@pytest.mark.parametrize("sample", BOTH, ids=lambda sample: sample["ticker"])
def test_deviation_report(sample, tmp_path, capsys):
    ticker = sample["ticker"]
    as_of = date.fromisoformat(str(sample["collected_at"]))
    ifind = golden_bundle(ticker)
    snapshot = build_snapshot_bundle(
        baostock_view(ticker.split(".")[0], as_of),
        company_name=ifind.pool["items"][0]["name"],
        industry=sample["industry"],  # same user text as the golden run
        retrieved_at=f"{as_of.isoformat()}T08:00:00+00:00",
        requested_report_year=int(MANIFEST["report_year"]),
    )
    rows = deviation_rows(ticker, ifind, snapshot, as_of=f"{as_of.isoformat()}T15:59:59+00:00")
    report = render_table(rows)
    (tmp_path / f"deviation_{ticker}.md").write_text(report, encoding="utf-8")
    with capsys.disabled():
        print(f"\nsnapshot vs iFinD deviation - {ticker} (as of {as_of})\n{report}")
    assert len(rows) == len(COMPARED_FIELDS) + len(COMPARED_SCORE_FIELDS)
