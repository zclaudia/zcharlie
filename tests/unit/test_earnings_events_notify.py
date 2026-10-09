"""``info`` notifications of the ``earnings_event`` job (mrd-intake C2): new forecasts
of the user's pools and upcoming appointed disclosures; the body cites ``raw_ref``;
a repeated capture of the same forecast is silent."""

from __future__ import annotations

from datetime import UTC, date, datetime

from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.earnings_events import collect_earnings_events
from investment_agent.snapshots.jobs import EarningsEventPayload
from investment_agent.snapshots.notifications import (
    notification_level,
    render_snapshot_notification,
)
from investment_agent.storage.models.analysis import PipelineRun
from tests.unit._eastmoney_fake import EVENTS_CAPTURED_AT, fake_client
from tests.unit._snapshot_support import seed_universe, seed_watchlist

AS_OF = date(2026, 10, 9)
RAW_REF = "snapshot-runs/9/attempt-1/earnings-forecast-2026-06-30.json"


def _run(metadata: dict[str, object], *, status: str = "success", part: str = "forecast") -> PipelineRun:
    return PipelineRun(
        id=9,
        kind="earnings_event",
        status=status,
        universe_code="CSI300",
        triggered_by="scheduler",
        started_at=datetime(2026, 10, 9, 11, tzinfo=UTC),
        request_json={
            "provider": "akshare", "part": part, "scope": "universe", "universe_code": "CSI300",
            "report_period": "2026-06-30", "as_of": AS_OF.isoformat(),
        },
        metadata_json=metadata,
    )


def _forecast(**overrides: object) -> dict[str, object]:
    return {
        "ticker": "002107.SZ", "name": "沃华医药", "report_period": "2026-06-30",
        "notice_date": "2026-07-07", "forecast_type": "预增", "indicator": "归属于上市公司股东的净利润",
        "profit_low": 65_000_000.0, "profit_high": 80_000_000.0, "yoy_low": 0.4549,
        "yoy_high": 0.7907, "pools": ["watchlist"], "raw_ref": RAW_REF, **overrides,
    }


def test_levels_new_forecasts_and_upcoming_disclosures_are_info():
    complete = {"completeness": {"ratio": 1.0, "expected": 8, "collected": 8}}
    assert notification_level(_run(complete), threshold=0.95) is None
    assert notification_level(
        _run({**complete, "new_forecasts": [_forecast()], "new_forecast_count": 1}), threshold=0.95
    ) == "info"
    assert notification_level(
        _run({**complete, "upcoming_disclosures": [{"ticker": "000856.SZ"}]}, part="schedule"),
        threshold=0.95,
    ) == "info"
    # Empty lists (second capture of the same notices) stay silent.
    assert notification_level(
        _run({**complete, "new_forecasts": [], "new_forecast_count": 0}), threshold=0.95
    ) is None
    # Failures and provider stops keep their action_required level.
    assert notification_level(_run({}, status="failed"), threshold=0.95) == "action_required"
    assert notification_level(
        _run({**complete, "stop_reason": "provider_unavailable", "new_forecasts": [_forecast()]}),
        threshold=0.95,
    ) == "action_required"


def test_render_lists_forecast_type_ranges_notice_date_and_raw_ref():
    run = _run(
        {
            "completeness": {"ratio": 1.0, "expected": 8, "collected": 8, "failed_count": 0,
                             "pending_count": 0},
            "new_forecasts": [
                _forecast(),
                _forecast(
                    ticker="000002.SZ", name="万科A", forecast_type="增亏", notice_date="2026-07-11",
                    profit_low=-15e9, profit_high=-12e9, yoy_low=-0.2556, yoy_high=-0.0045,
                    pools=["watchlist", "holding"],
                ),
                _forecast(
                    ticker="600030.SH", name="中信证券", profit_low=23_343_000_000.0,
                    profit_high=23_343_000_000.0, yoy_low=0.7015, yoy_high=0.7015, pools=["candidate"],
                ),
            ],
            "new_forecast_count": 3,
        }
    )
    text = render_snapshot_notification(run, "info", threshold=0.95)
    assert "# 财报事件 · CSI300 · 2026-10-09" in text and "`info`" in text
    assert "## 新业绩预告（3）" in text
    assert "002107.SZ 沃华医药（观察池）· 预增 · 归属于上市公司股东的净利润 6,500 万元 ~ 8,000 万元" in text
    assert "同比 +45.5% ~ +79.1% · 公告 2026-07-07 · 报告期 2026-06-30" in text
    assert "000002.SZ 万科A（观察池 / 持仓）· 增亏" in text
    assert "-150.00 亿元 ~ -120.00 亿元 · 同比 -25.6% ~ -0.4%" in text
    # A point estimate is not rendered as a range.
    assert "233.43 亿元 · 同比 +70.2% · 公告 2026-07-07" in text
    assert text.count(f"来源 `{RAW_REF}`") == 3
    assert "预约披露临近" not in text


def test_render_upcoming_disclosures():
    ref = "snapshot-runs/9/attempt-1/earnings-schedule-2026-09-30.json"
    run = _run(
        {
            "upcoming_disclosures": [
                {"ticker": "000856.SZ", "name": "冀东装备", "report_period": "2026-09-30",
                 "scheduled_date": "2026-10-13", "first_date": "2026-10-13",
                 "trading_days_ahead": 2, "pools": ["candidate"], "raw_ref": ref},
                {"ticker": "002107.SZ", "name": "沃华医药", "report_period": "2026-09-30",
                 "scheduled_date": "2026-10-10", "first_date": "2026-10-10",
                 "trading_days_ahead": 0, "pools": ["watchlist"], "raw_ref": ref},
            ],
            "upcoming_count": 2,
        },
        part="schedule",
    )
    text = render_snapshot_notification(run, "info", threshold=0.95)
    assert "## 预约披露临近（2）" in text
    assert "000856.SZ 冀东装备（候选）· 预约 2026-10-13（2 个交易日后）· 报告期 2026-09-30" in text
    assert "002107.SZ 沃华医药（观察池）· 预约 2026-10-10（下一交易日前）" in text
    assert text.count(f"来源 `{ref}`") == 2


async def test_only_the_first_capture_of_a_forecast_notifies(db_session, tmp_path):
    del db_session
    await seed_universe(["600519.SH", "600030.SH"])
    await seed_watchlist(["002107.SZ", "000002.SZ"])
    payload = EarningsEventPayload(
        part="forecast", universe_code="CSI300", report_period=date(2026, 6, 30), as_of=AS_OF
    )
    client = fake_client(clock=EVENTS_CAPTURED_AT)

    async def collect(run_id: int):
        report = await collect_earnings_events(
            payload,
            client=client,
            artifacts=ArtifactWriter(tmp_path, run_id=run_id, attempt=1),
            lease=None,
            now=lambda: EVENTS_CAPTURED_AT,
        )
        return _run(report.to_metadata(), status=report.status)

    first = await collect(1)
    assert notification_level(first, threshold=0.95) == "info"
    body = render_snapshot_notification(first, "info", threshold=0.95)
    assert "002107.SZ" in body and "000002.SZ" in body and "600030.SH" not in body
    assert "snapshot-runs/1/attempt-1/earnings-forecast-2026-06-30.json" in body
    second = await collect(2)
    assert notification_level(second, threshold=0.95) is None
    assert second.metadata_json["existing"] == 3 and second.metadata_json["new_forecasts"] == []
