from investment_agent.research.ifind_chain import (
    EvidenceClaim,
    ResearchExplanation,
    _return_stats,
    parse_proxy_rows,
    validate_explanation_fields,
)


def test_parse_proxy_rows_unwraps_data_preview_csv():
    body = {
        "format": "json",
        "data": {"data_preview": "code,close\n600406.SH,22.52\n"},
    }

    assert parse_proxy_rows(body) == [{"code": "600406.SH", "close": "22.52"}]


def test_return_stats_are_deterministic():
    rows = [
        {"time": "20260101", "close": "10"},
        {"time": "20260102", "close": "12"},
        {"time": "20260103", "close": "9"},
    ]

    total_return, volatility, drawdown = _return_stats(rows)

    assert total_return == -0.1
    assert volatility is not None and volatility > 0
    assert drawdown == 0.25


def test_explanation_rejects_unknown_canonical_field():
    explanation = ResearchExplanation(
        summary="summary",
        classification_rationale=[
            EvidenceClaim(text="claim", supporting_fields=["key_metrics.not_real"])
        ],
        positive_evidence=[],
        negative_evidence=[],
        risk_factors=[],
        data_quality_notes=[],
        open_questions=[],
        workflow_conclusion="needs_review",
    )

    try:
        validate_explanation_fields(explanation, {"key_metrics": {"roe": 0.16}})
    except ValueError as exc:
        assert "key_metrics.not_real" in str(exc)
    else:
        raise AssertionError("unknown field should fail validation")
