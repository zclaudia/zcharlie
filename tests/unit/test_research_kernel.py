"""Kernel input validation tests.

The golden parity coverage lives in ``test_golden_corpus_replay.py`` and is
driven by ``tests/fixtures/research_kernel/manifest.yaml``.
"""

from pathlib import Path

from investment_agent.research.kernel import InProcessResearchKernel


def test_in_process_kernel_rejects_non_object_input(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]", encoding="utf-8")

    try:
        InProcessResearchKernel().score_and_render(
            pool=invalid,
            market=invalid,
            financial=invalid,
            artifact_dir=tmp_path,
        )
    except ValueError as exc:
        assert "must be a JSON object" in str(exc)
    else:
        raise AssertionError("non-object input should be rejected")


def test_in_process_kernel_writes_all_artifacts(tmp_path: Path):
    fixture = Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"
    artifacts = InProcessResearchKernel().score_and_render(
        pool=fixture / "pool.json",
        market=fixture / "market.json",
        financial=fixture / "financial.json",
        artifact_dir=tmp_path,
        as_of="2026-10-05T09:46:20.080336+00:00",
    )

    assert artifacts.scores_json.is_file()
    assert artifacts.scores_csv.is_file()
    assert artifacts.report_context_json.is_file()
    assert artifacts.report_markdown.is_file()
