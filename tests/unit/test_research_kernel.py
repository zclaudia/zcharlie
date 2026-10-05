import json
from pathlib import Path
from typing import Any, cast

from investment_agent.research.kernel import InProcessResearchKernel

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "research_kernel" / "600406"


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def test_in_process_kernel_matches_legacy_golden_output(tmp_path):
    expected_scores = _read_json(FIXTURE_ROOT / "legacy_scores.json")
    expected_context = _read_json(FIXTURE_ROOT / "legacy_report_context.json")
    as_of = expected_scores["metadata"]["freshness_policy"]["as_of"]

    artifacts = InProcessResearchKernel().score_and_render(
        pool=FIXTURE_ROOT / "pool.json",
        market=FIXTURE_ROOT / "market.json",
        financial=FIXTURE_ROOT / "financial.json",
        artifact_dir=tmp_path,
        as_of=as_of,
    )

    actual_scores = _read_json(artifacts.scores_json)
    actual_context = _read_json(artifacts.report_context_json)

    assert actual_scores["items"] == expected_scores["items"]
    assert actual_scores["metadata"] == {
        **expected_scores["metadata"],
        "generated_at": actual_scores["metadata"]["generated_at"],
    }
    assert actual_context["items"] == expected_context["items"]
    assert artifacts.scores_csv.read_text(encoding="utf-8").startswith("symbol,name,strategy_label")
    assert artifacts.report_markdown.read_text(encoding="utf-8").startswith("# 股票研究上下文")


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
