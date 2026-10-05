"""Research change notification rendering tests."""

from investment_agent.research.change_notification import render_change_notification


def test_notification_renders_only_meaningful_changes():
    content = render_change_notification(
        ticker="600406.SH",
        company_name="国电南瑞",
        current_run_id=2,
        previous_run_id=1,
        diff={
            "changes": [
                {
                    "category": "score",
                    "field": "business_score",
                    "before": 50.0,
                    "after": 53.0,
                    "meaningful": True,
                },
                {
                    "category": "financial_metric",
                    "field": "key_metrics.pe",
                    "before": 20.0,
                    "after": 20.1,
                    "meaningful": False,
                },
            ]
        },
    )

    assert "`business_score`：50.0 → 53.0" in content.body
    assert "key_metrics.pe" not in content.body
    assert "重要变化：1 项" in content.body
    assert content.metadata == {"run_id": 2, "ticker": "600406.SH"}
