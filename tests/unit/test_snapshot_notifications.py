"""Notification level decisions for finished snapshot runs."""

from __future__ import annotations

from datetime import UTC, datetime

from investment_agent.snapshots.notifications import (
    notification_level,
    render_snapshot_notification,
)
from investment_agent.storage.models.analysis import PipelineRun


def _run(kind: str, status: str, metadata: dict[str, object] | None = None) -> PipelineRun:
    return PipelineRun(
        id=9,
        kind=kind,
        status=status,
        universe_code="CSI300",
        triggered_by="scheduler",
        started_at=datetime(2026, 10, 6, tzinfo=UTC),
        request_json={"universe_code": "CSI300", "trade_date": "2026-10-06"},
        metadata_json=metadata or {},
    )


def _completeness(ratio: float) -> dict[str, object]:
    return {"completeness": {"ratio": ratio, "expected": 300, "collected": int(300 * ratio)}}


def test_levels():
    assert notification_level(_run("market_snapshot", "failed"), threshold=0.95) == (
        "action_required"
    )
    assert notification_level(
        _run("market_snapshot", "partial", _completeness(0.9)), threshold=0.95
    ) == "action_required"
    # One suspended stock out of 300 is fine.
    assert notification_level(
        _run("market_snapshot", "success", _completeness(0.9967)), threshold=0.95
    ) is None
    assert notification_level(
        _run("financial_snapshot", "partial", _completeness(0.97)), threshold=0.95
    ) is None
    assert notification_level(
        _run("universe_sync", "success", {"changed": True, "added": ["600000.SH"]}),
        threshold=0.95,
    ) == "info"
    assert notification_level(
        _run("universe_sync", "success", {"changed": False}), threshold=0.95
    ) is None
    assert notification_level(_run("industry_sync", "failed"), threshold=0.95) == (
        "action_required"
    )
    assert notification_level(_run("ifind_research", "failed"), threshold=0.95) is None
    assert notification_level(_run("market_snapshot", "running"), threshold=0.95) is None


def test_render_lists_missing_tickers():
    run = _run(
        "market_snapshot",
        "partial",
        {
            "completeness": {
                "ratio": 0.5,
                "expected": 2,
                "collected": 1,
                "failed_count": 1,
                "pending_count": 0,
                "missing": {"600000.SH": {"kind": "failed", "reason": "none: ValueError"}},
            }
        },
    )
    text = render_snapshot_notification(run, "action_required", threshold=0.95)
    assert "`action_required`" in text
    assert "600000.SH" in text and "ValueError" in text
    assert "2026-10-06" in text
