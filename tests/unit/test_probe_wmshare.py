"""Exercise the experiment with synthetic SDK responses and recorded replay."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("probe_wmshare", ROOT / "tools/probe_wmshare.py")
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)

AS_OF = date(2026, 11, 12)
EXTRAS = [f"002{i:03d}.SZ" for i in range(1, 9)]
INDUSTRIES = [
    {"symbol": "S48", "name": "银行"},
    {"symbol": "S34", "name": "食品饮料"},
    {"symbol": "S41", "name": "公用事业"},
    {"symbol": "S27", "name": "电子"},
]


class FakeClient:
    """Deterministic SDK-shaped fixture, never a source of live market facts."""

    def __init__(self, *, fail_history: str | None = None, truncate: bool = False):
        self.last_ec_cost = None
        self.calls = []
        self.fail_history = fail_history
        self.truncate = truncate
        self.stocks = SimpleNamespace(list=self.directory, industries=lambda **_: INDUSTRIES)
        self.screener = SimpleNamespace(query=self.query)
        self.index = SimpleNamespace(universe_keys=self.keys, trend=self.trend)
        self.indicator = SimpleNamespace(metrics=self.metrics)
        self.dividend = SimpleNamespace(
            snapshot=self.snapshot,
            history=self.history,
            yield_ttm=lambda **_: 0.04,
            payout_ttm=lambda **_: 0.5,
        )
        self.kline = SimpleNamespace(last=self.bar)

    def directory(self):
        return [
            {
                "symbol": s.split(".")[0],
                "exchange": s.split(".")[1].lower(),
                "name": f"fixture-{s}",
                "industry": "industry.S340501",
                "industry_name": "白酒",
            }
            for s in [*probe.FIXED, *EXTRAS, "900948.SH", "510300.SH"]
        ]

    def values(self):
        return {
            "$ROE_TTM_LAST": 0.2,
            "$NCFO_NP": 1.2,
            "$PE_TTM_LAST": 12.0,
            "$PCT_PE_TTM_10Y_LAST": 0.2,
            "$IS_ST": False,
            "$IS_SUSPEND": False,
            "$INDUSTRY_CUR": "白酒",
            "$MV_LAST": 1e10,
            "$BONUS_RATE_3Y_AVG_LAST": 0.035,
        }

    def query(self, **params):
        self.calls.append(("screener.query", params))
        self.last_ec_cost = 25
        symbols = ["900948", "510300", *[x[:6] for x in EXTRAS]]
        return {
            "count": 250,
            "rows": [{"symbol": s, "name": f"fixture-{s}", **self.values()} for s in symbols],
        }

    def keys(self, group):
        return (
            [f"industry.{r['symbol']}" for r in INDUSTRIES]
            if group == "industry"
            else ["index.hs300"]
        )

    def trend(self, key, window):
        points = [
            {"date": AS_OF.isoformat(), "pe": 20.0, "pb": 2.0},
            {"date": "2024-11-12", "pe": 10.0, "pb": 1.0},
            {"date": "2022-11-12", "pe": 20.0, "pb": 2.0},
            {"date": "2021-11-11", "pe": 200.0, "pb": 20.0},
        ]
        return {
            "ok": True,
            "key": key,
            "as_of": AS_OF.isoformat(),
            "window": window,
            "points": points,
            "win_start": "2021-11-12",
            "win_trunc": self.truncate,
            "pe_pct_window": "5y",
            "pe_pct": 0.75,
            "pb_pct": 0.75,
            "pct": {"pe": {"p3": 0.5, "p5": 0.75, "p10": None}},
        }

    def metrics(self, symbol, codes):
        self.calls.append(("indicator.metrics", symbol))
        # Does not set EC: the recorder must not retain the earlier query's cost.
        return {code: self.values().get(code) for code in codes}

    def snapshot(self, sym):
        return {"yield_ttm": 0.04, "payout_ttm": 0.5}

    def history(self, symbol, begin=None, end=None):
        if symbol in (self.fail_history, self.fail_history[:6] if self.fail_history else None):
            raise RuntimeError("fixture history unavailable")
        return [
            {"ex_date": f"{year}-06-15", "dps": 0.4, "pay": f"{year}-06-15"}
            for year in range(2022, 2027)
        ]

    def bar(self, symbol):
        return {"close": 10.0, "kline_date": AS_OF.isoformat()}


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_directory_filters_b_shares_and_etf_without_losing_bse():
    assert not probe.is_a_share({"symbol": "900948", "exchange": "sh"})
    assert not probe.is_a_share({"symbol": "200002", "exchange": "sz"})
    assert not probe.is_a_share({"symbol": "510300", "exchange": "sh"})
    assert not probe.is_a_share({"symbol": "600519", "exchange": "sz"})
    assert probe.is_a_share({"symbol": "920001", "exchange": "bj"})
    assert probe.is_a_share({"symbol": "688111", "exchange": "sh"})


def test_percentile_uses_latest_date_and_midrank():
    points = [
        {"date": "2022-01-01", "pe": 30.0},
        {"date": "2026-11-12", "pe": 20.0},
        {"date": "2024-01-01", "pe": 20.0},
        {"date": "2023-01-01", "pe": 10.0},
    ]
    assert probe.rank_percentile(points, "pe") == pytest.approx(0.5)
    assert probe.rank_percentile([{"date": "2026-01-01", "pe": None}], "pe") is None


def test_screen_values_must_establish_conditions():
    values = FakeClient().values()
    assert probe.screen_check("profit_cash", values) == "supported"
    assert probe.screen_check("low_valuation", {**values, "$PE_TTM_LAST": -5}) == "contradiction"
    assert probe.screen_check("profit_cash", {**values, "$IS_ST": True}) == "contradiction"
    assert probe.screen_check("profit_cash", {**values, "$NCFO_NP": None}) == "unknown"


def test_event_window_excludes_proposals_and_marks_share_actions():
    begin = AS_OF - timedelta(days=1825)
    rows = [
        {"ex_date": (AS_OF - timedelta(days=365)).isoformat(), "dps": 9.0},
        {"ex_date": (AS_OF - timedelta(days=364)).isoformat(), "dps": 0.2},
        {"ex_date": AS_OF.isoformat(), "dps": 0.3, "content": "送股"},
        {"ex_date": AS_OF.isoformat(), "dps": 5.0, "status": "预案"},
        {"dps": 7.0, "status": "预案"},
        {"ex_date": (AS_OF + timedelta(days=1)).isoformat(), "dps": 8.0},
    ]
    years, recent, proposals, actions = probe.dividend_events(rows, begin, AS_OF)
    assert recent == pytest.approx(0.5)
    assert sum(years.values()) == pytest.approx(9.5)
    assert len(proposals) == 2
    assert actions


def test_missing_dividend_amount_is_not_a_zero_payment():
    years, recent, _, _ = probe.dividend_events(
        [
            {"ex_date": "2026-06-15", "dps": 0.2},
            {"ex_date": "2026-07-15", "dps": None},
            {"ex_date": "2026-08-15", "dps": 0.3},
        ],
        AS_OF - timedelta(days=1825),
        AS_OF,
    )
    assert years["2026"] is None
    assert recent is None


def test_missing_ex_div_cannot_fall_back_to_announcement_as_cash_payment():
    rows = [{"ex_div": None, "ex_date": "2026-08-28", "pay": None, "dps": 0.09}]
    years, recent, pending, _ = probe.dividend_events(rows, AS_OF - timedelta(days=1825), AS_OF)
    assert years == {} and recent == 0
    assert pending == rows


def test_end_to_end_records_and_replays_without_client(tmp_path):
    first, second = tmp_path / "recorded", tmp_path / "replayed"
    client = FakeClient()
    result = probe.run_probe(first, None, AS_OF, client=client)
    assert len(result["samples"]) == 20
    assert set(EXTRAS) <= set(result["samples"])
    assert len(_rows(first / "industry.csv")) == 5
    assert len(_rows(first / "dividends.csv")) == 20
    assert all(
        r["symbol"] not in ("900948.SH", "510300.SH") for r in _rows(first / "discovery.csv")
    )
    assert result["dividends"][0]["price_date"] == AS_OF.isoformat()
    assert result["dividends"][0]["industry_background"] == "食品饮料"
    assert result["dividends"][0]["event_yield_ttm"] == pytest.approx(0.04)
    assert result["industry"][0]["pe_recomputed"] == pytest.approx(0.75)
    assert len([c for c in client.calls if c[0] == "indicator.metrics"]) == 20
    assert all(r["ec"] is None for r in result["records"] if r["endpoint"] == "indicator.metrics")

    class NoCalls:
        def __getattr__(self, name):
            pytest.fail(f"replay tried to call a client: {name}")

    replayed = probe.run_probe(second, None, AS_OF, client=NoCalls(), replay=first)
    for filename in ("discovery.csv", "industry.csv", "dividends.csv"):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
    assert not [r for r in replayed["records"] if not r["success"]]
    assert "回放" in (second / "summary.md").read_text()
    assert json.loads((second / "run.json").read_text())["source_mode"] == "injected"


def test_partial_history_failure_does_not_become_zero(tmp_path):
    result = probe.run_probe(tmp_path, None, AS_OF, client=FakeClient(fail_history=probe.FIXED[0]))
    row = result["dividends"][0]
    assert row["annual_dps"] is None
    assert row["recent_365d_dps"] is None
    assert len(result["industry"]) == 5
    assert "fixture history unavailable" in (tmp_path / "summary.md").read_text()


def test_truncated_window_is_not_treated_as_verified_five_years(tmp_path):
    result = probe.run_probe(tmp_path, None, AS_OF, client=FakeClient(truncate=True))
    assert all(r["pe_recomputed"] is None for r in result["industry"])
    assert all(r["missing_reason"] for r in result["industry"])


def test_missing_industry_date_or_percentile_needs_followup(tmp_path):
    client = FakeClient()
    original = client.index.trend

    def incomplete(**kwargs):
        return {**original(**kwargs), "as_of": None, "pe_pct": None, "pb_pct": None}

    client.index.trend = incomplete
    result = probe.run_probe(tmp_path, None, AS_OF, client=client)
    assert all("日期未知" in row["missing_reason"] for row in result["industry"])
    assert "补查行业目录、趋势覆盖和分位差异" in (tmp_path / "summary.md").read_text()


def test_dividend_judgment_does_not_call_low_yield_high():
    row = {
        "annual_dps": {str(y): 0.01 for y in range(2022, 2027)},
        "history_status": "available",
        "yield_ttm": 0.001,
        "payout_ttm": 0.5,
        "recent_365d_dps": 0.01,
    }
    assert "未达到" in probe.dividend_judgment(row, AS_OF - timedelta(days=1825), AS_OF)


def test_cli_missing_credentials_is_actionable_and_does_not_create_output(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("WMSHARE_CLIENT_ID", raising=False)
    monkeypatch.delenv("WMSHARE_CLIENT_SECRET", raising=False)
    output = tmp_path / "missing"
    assert probe.main(["--output", str(output)]) == 2
    message = capsys.readouterr().err
    assert "WMSHARE_CLIENT_ID" in message and "WMSHARE_CLIENT_SECRET" in message
    assert "Traceback" not in message
    assert not output.exists()


def test_recorded_failure_redacts_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("WMSHARE_CLIENT_SECRET", "fixture-private-secret")

    def fail(**_):
        raise RuntimeError("upstream reflected fixture-private-secret")

    client = SimpleNamespace(last_ec_cost=None, stocks=SimpleNamespace(list=fail))
    recorder = probe.Recorder(client, tmp_path)
    assert recorder.call("stocks.list") is None
    assert "fixture-private-secret" not in json.dumps(recorder.records)


@pytest.mark.parametrize("error_name", ["AuthError", "QuotaError"])
def test_auth_or_quota_failure_stops_remaining_network_calls(tmp_path, error_name):
    calls = []
    error_class = type(error_name, (Exception,), {})

    def fail(**_):
        calls.append(1)
        raise error_class("fixture credential or quota failure")

    recorder = probe.Recorder(SimpleNamespace(stocks=SimpleNamespace(list=fail)), tmp_path)
    assert recorder.call("stocks.list") is None
    assert recorder.call("stocks.list") is None
    assert len(calls) == 1
    assert len(recorder.records) == 1


def test_policy_denial_blocks_only_affected_endpoint(tmp_path):
    error_class = type("AuthError", (Exception,), {})

    def denied(**_):
        error = error_class("POLICY_DENIED: assign")
        error.code, error.status = "POLICY_DENIED", 403
        raise error

    client = SimpleNamespace(
        screener=SimpleNamespace(query=denied),
        stocks=SimpleNamespace(list=lambda: [{"symbol": "600519"}]),
    )
    recorder = probe.Recorder(client, tmp_path)
    assert recorder.call("screener.query", where="first") is None
    assert recorder.call("screener.query", where="second") is None
    assert recorder.call("stocks.list") == [{"symbol": "600519"}]
    assert len(recorder.records) == 2


def test_resume_reuses_expensive_success_and_continues_after_old_policy_error(tmp_path):
    source = tmp_path / "initial"
    source.mkdir()
    first = probe.Recorder(FakeClient(), source)
    expected = first.call("stocks.list")
    (source / "raw" / "002.json").write_text(
        json.dumps(
            {
                "endpoint": "screener.query",
                "params": {"where": "first"},
                "result": None,
                "success": False,
                "error": "AuthError: POLICY_DENIED: assign",
                "ec": None,
            }
        )
    )
    output = tmp_path / "continuation"
    second = probe.Recorder(
        SimpleNamespace(kline=SimpleNamespace(last=lambda: {"close": 10})), output, resume=source
    )
    assert second.call("stocks.list") == expected
    assert second.records[0]["reused"] is True
    assert second.call("screener.query", where="first") is None
    assert second.call("screener.query", where="second") is None
    assert second.call("kline.last") == {"close": 10}


def test_live_resume_retries_previous_quota_failure(tmp_path):
    source = tmp_path / "previous"
    source.mkdir()
    (source / "quota.json").write_text(
        json.dumps(
            {
                "endpoint": "kline.last",
                "params": {},
                "success": False,
                "result": None,
                "error": "QuotaError: EC_BALANCE_EXHAUSTED",
                "error_code": "EC_BALANCE_EXHAUSTED",
            }
        )
    )
    client = SimpleNamespace(kline=SimpleNamespace(last=lambda: {"close": 10}))
    recorder = probe.Recorder(client, tmp_path / "next", resume=source)
    assert recorder.call("kline.last") == {"close": 10}
    assert recorder.records[0]["reused"] is False


def test_sdk_validation_error_preserves_reported_ec_cost(tmp_path):
    client = SimpleNamespace(last_ec_cost=None)

    def failed_guard():
        client.last_ec_cost = 123
        raise ValueError("bad provider value after metering")

    client.stocks = SimpleNamespace(list=failed_guard)
    recorder = probe.Recorder(client, tmp_path)
    assert recorder.call("stocks.list") is None
    assert recorder.records[0]["ec"] == 123


def test_dividends_scope_avoids_discovery_industry_and_writes_empty_tables(tmp_path):
    client = FakeClient()

    def forbidden(**kwargs):
        pytest.fail(f"unselected experiment requested: {kwargs}")

    client.stocks.list = forbidden
    client.stocks.industries = forbidden
    client.screener.query = forbidden
    client.index.universe_keys = forbidden
    client.index.trend = forbidden
    result = probe.run_probe(
        tmp_path, None, AS_OF, client=client, experiments=["dividends"], samples=["600519.SH"]
    )
    assert result["samples"] == ["600519.SH"]
    assert len(result["dividends"]) == 1
    assert _rows(tmp_path / "industry.csv") == []
    assert _rows(tmp_path / "discovery.csv") == []
    assert not (tmp_path / "industry-trend.png").exists()
    summary = (tmp_path / "summary.md").read_text()
    assert summary.count("未选择实验") == 2
    assert all(not r["endpoint"].startswith(("screener.", "index.")) for r in result["records"])


def test_policy_retry_only_once_per_endpoint_after_resume(tmp_path):
    source = tmp_path / "previous"
    source.mkdir()
    (source / "denied.json").write_text(
        json.dumps(
            {
                "endpoint": "screener.query",
                "params": {"where": "old"},
                "success": False,
                "result": None,
                "error": "AuthError: POLICY_DENIED",
                "error_code": "POLICY_DENIED",
            }
        )
    )
    calls = []
    error_class = type("AuthError", (Exception,), {})

    def denied(**params):
        calls.append(params)
        error = error_class("POLICY_DENIED")
        error.code = "POLICY_DENIED"
        raise error

    client = SimpleNamespace(screener=SimpleNamespace(query=denied))
    recorder = probe.Recorder(client, tmp_path / "next", resume=source, retry_policy=True)
    assert recorder.call("screener.query", where="new") is None
    assert recorder.call("screener.query", where="another") is None
    assert calls == [{"where": "new"}]
    assert len(recorder.records) == 1 and not recorder.records[0]["reused"]

    default = probe.Recorder(client, tmp_path / "default", resume=source)
    assert default.call("screener.query", where="different") is None
    assert len(calls) == 1


def test_snapshot_dates_and_zero_dps_placeholder(tmp_path):
    client = FakeClient()
    client.dividend.snapshot = lambda sym: {
        "yield_ttm": 0.04,
        "payout_ttm": 0.5,
        "quote_date": "2026-11-11",
        "dps_ttm": 0.4,
        "bonus_ttm": 400,
        "mv": 10000,
    }
    client.dividend.history = lambda **kw: [{"ex_date": "2026-06-30", "dps": 0, "pay": None}]
    result = probe.run_probe(
        tmp_path, None, AS_OF, client=client, experiments=["dividends"], samples=["600519.SH"]
    )
    row = result["dividends"][0]
    assert row["snapshot_quote_date"] == "2026-11-11"
    assert row["snapshot_dps_ttm"] == 0.4
    assert row["history_status"] == "empty/unconfirmed"
    assert row["annual_dps"] is None
    assert row["snapshot_event_yield_difference"] is None


def test_cash_yield_window_uses_snapshot_quote_date_and_scoped_replay(tmp_path):
    client = FakeClient()
    client.dividend.snapshot = lambda sym: {
        "yield_ttm": 0.04,
        "payout_ttm": 0.5,
        "quote_date": "2026-11-11",
    }
    client.dividend.history = lambda **kw: [
        {"ex_date": "2025-11-12", "ex_div": "2025-11-12", "dps": 0.4},
    ]
    client.kline.last = lambda **kw: {"date": "2026-11-11", "close": 10}
    first, second = tmp_path / "first", tmp_path / "second"
    result = probe.run_probe(
        first, None, AS_OF, client=client, experiments=["dividends"], samples=["600519.SH"]
    )
    row = result["dividends"][0]
    assert row["event_window_end"] == "2026-11-11"
    assert row["recent_365d_dps"] == 0.4
    assert row["snapshot_event_yield_difference"] == 0
    replay = probe.run_probe(second, None, AS_OF, replay=first)
    assert replay["samples"] == ["600519.SH"]
    assert len(replay["records"]) == len(result["records"])
    assert (first / "dividends.csv").read_bytes() == (second / "dividends.csv").read_bytes()
