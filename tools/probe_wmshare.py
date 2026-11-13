#!/usr/bin/env python3
"""Small, recorded wmshare experiment; replay never creates an SDK client."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FIXED = [
    "600036.SH",
    "601398.SH",
    "601288.SH",
    "600519.SH",
    "000858.SZ",
    "600900.SH",
    "601088.SH",
    "600028.SH",
    "300750.SZ",
    "002415.SZ",
    "688111.SH",
    "600029.SH",
]
FIELDS = [
    "$ROE_TTM_LAST",
    "$NCFO_NP",
    "$PE_TTM_LAST",
    "$PCT_PE_TTM_10Y_LAST",
    "$IS_ST",
    "$IS_SUSPEND",
    "$INDUSTRY_CUR",
    "$MV_LAST",
]
CODES = FIELDS + ["$BONUS_RATE_3Y_AVG_LAST"]
SCREENS = [
    (
        "profit_cash",
        "$ROE_TTM_LAST > 0.15 && $NCFO_NP > 1 && !$IS_ST && !$IS_SUSPEND",
        "$ROE_TTM_LAST",
        True,
    ),
    (
        "low_valuation",
        "$PE_TTM_LAST > 0 && $PCT_PE_TTM_10Y_LAST < 0.3 && $ROE_TTM_LAST > 0.08 && !$IS_ST && !$IS_SUSPEND",
        "$PCT_PE_TTM_10Y_LAST",
        False,
    ),
]
NAMES = ["银行", "食品饮料", "公用事业", "电子", "沪深300"]


def ticker(value):
    s = str(value or "").upper().strip()
    m = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", s)
    if not m:
        return ""
    code, exchange = m.groups()
    if exchange is None:
        exchange = (
            "BJ"
            if code.startswith(("4", "8", "92"))
            else "SH"
            if code.startswith(("6", "9"))
            else "SZ"
        )
    return f"{code}.{exchange}"


def is_a_share(row):
    """Require a directory match and mainland A-share code/exchange conventions."""
    t = ticker(row.get("symbol"))
    if not t:
        return False
    code, ex = t.split(".")
    declared = str(row.get("exchange") or "").upper()
    if declared and declared not in (
        ex,
        {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}[ex],
        {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBSE"}[ex],
    ):
        return False
    category = str(row.get("type") or row.get("category") or "").lower()
    if any(x in category for x in ("fund", "etf", "基金", "b股", "b-share")):
        return False
    return (
        (ex == "SH" and code.startswith(("600", "601", "603", "605", "688", "689")))
        or (ex == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301")))
        or (ex == "BJ" and code.startswith(("4", "8", "92")))
    )


def number(v):
    return (
        float(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        else None
    )


def rank_percentile(points, metric):
    """Midrank rank0/(n-1), using the latest point and positive observations."""
    ordered = sorted(
        (p for p in points if p.get("date")), key=lambda p: str(p["date"]), reverse=True
    )
    if not ordered:
        return None
    current = number(ordered[0].get(metric))
    values = [number(p.get(metric)) for p in ordered]
    values = [v for v in values if v is not None and v > 0]
    if current is None or current <= 0 or len(values) < 2 or current not in values:
        return None
    lo = sum(v < current for v in values)
    return (lo + (values.count(current) - 1) / 2) / (len(values) - 1)


def screen_check(label, row):
    """Return whether available values establish the requested screen conditions."""
    fields = ["$ROE_TTM_LAST", "$IS_ST", "$IS_SUSPEND"]
    fields += ["$NCFO_NP"] if label == "profit_cash" else ["$PE_TTM_LAST", "$PCT_PE_TTM_10Y_LAST"]
    if any(row.get(k) is None for k in fields):
        return "unknown"
    roe = number(row["$ROE_TTM_LAST"])
    if roe is None or row["$IS_ST"] or row["$IS_SUSPEND"]:
        return "contradiction"
    if label == "profit_cash":
        ncfo = number(row["$NCFO_NP"])
        ok = ncfo is not None and roe > 0.15 and ncfo > 1
    else:
        pe, pct = number(row["$PE_TTM_LAST"]), number(row["$PCT_PE_TTM_10Y_LAST"])
        ok = pe is not None and pct is not None and pe > 0 and pct < 0.3 and roe > 0.08
    return "supported" if ok else "contradiction"


def dividend_events(rows, begin, end):
    """Return dated implemented DPS and separate undated/proposed rows."""
    years, recent, proposals, actions = {}, 0.0, [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # SDK ex_date can fall back to the announcement date.
        if "ex_div" in row and not row["ex_div"]:
            if number(row.get("dps")) != 0:
                proposals.append(row)
            continue
        ds = str(row.get("ex_div") or row.get("ex_date") or "")[:10]
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            proposals.append(row)
            continue
        if not begin <= d <= end:
            continue
        status = str(row.get("status") or row.get("stage") or "")
        if any(x in status.lower() for x in ("预案", "proposal", "取消", "cancel")):
            proposals.append(row)
            continue
        dps = number(row.get("dps"))
        # A zero-DPS dated placeholder is not evidence of a cash payment.
        if dps == 0:
            continue
        year = str(d.year)
        if dps is None:
            years[year] = None
            if d > end - timedelta(days=365):
                recent = None
        else:
            if years.get(year, 0) is not None:
                years[year] = years.get(year, 0) + dps
            if d > end - timedelta(days=365) and recent is not None:
                recent += dps
        content = str(row.get("content") or "")
        if any(x in content for x in ("转增", "送股", "拆股", "split", "bonus shares")):
            actions.append(f"{ds}: {content}")
    return years, recent, proposals, actions


def dividend_judgment(row, begin, end):
    annual = row["annual_dps"] or {}
    full_years = [str(y) for y in range(begin.year + 1, end.year)]
    payout, current_yield = number(row["payout_ttm"]), number(row["yield_ttm"])
    if (
        row["history_status"] != "available"
        or len(full_years) < 3
        or payout is None
        or any(v is None for v in annual.values())
    ):
        return "资料不足"
    difference = number(row.get("snapshot_event_yield_difference"))
    if difference is not None and abs(difference) > 0.003:
        return "平台 TTM 与已实施现金股息口径差异需核对"
    if payout > 1:
        return "平台派息率超过 100%，需结合利润与分红口径核对"
    if current_yield is None or current_yield < 0.03:
        return "当前股息率未达到本实验的 3% 高股息观察线"
    if all(annual.get(y, 0) > 0 for y in full_years):
        return "高且持续（窗口内各完整年度均记录到现金分红）"
    recent = number(row["recent_365d_dps"])
    if recent and annual and recent >= sum(annual.values()) * 0.7:
        return "近期分红集中，需核查一次性因素"
    return "分红记录有间断，需结合上市日期核查"


class Recorder:
    def __init__(self, client, output, replay=None, resume=None, retry_policy=False):
        self.stop_reason = None
        self.blocked_endpoints = {}
        self.retry_policy = retry_policy and not replay
        self.policy_retried = set()
        self.policy_cleared = set()
        self.client, self.output, self.records, self.source = (
            None if replay else client,
            output,
            [],
            [],
        )
        if replay or resume:
            path = Path(replay or resume)
            path = path / "raw" if (path / "raw").is_dir() else path
            files = sorted(path.glob("*.json")) if path.is_dir() else [path]
            for f in files:
                data = json.loads(f.read_text(encoding="utf-8"))
                self.source.extend(data if isinstance(data, list) else [data])

    def call(self, endpoint, **params):
        if self.stop_reason:
            return None
        if endpoint in self.blocked_endpoints:
            return None
        started = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        start = time.monotonic()
        result, error, ec = None, None, None
        match = next(
            (r for r in self.source if r.get("endpoint") == endpoint and r.get("params") == params),
            None,
        )
        if match is None and self.client is not None:
            match = next(
                (
                    r
                    for r in self.source
                    if r.get("endpoint") == endpoint
                    and not r.get("success")
                    and (
                        str(r.get("error_code", "")).upper() == "POLICY_DENIED"
                        or "POLICY_DENIED" in (r.get("error") or "")
                    )
                ),
                None,
            )
        denied = (
            match
            and not match.get("success")
            and (
                str(match.get("error_code", "")).upper() == "POLICY_DENIED"
                or "POLICY_DENIED" in (match.get("error") or "")
            )
        )
        if denied and self.client is not None and self.retry_policy:
            if endpoint in self.policy_cleared:
                match = None
            elif endpoint not in self.policy_retried:
                self.policy_retried.add(endpoint)
                match = None
        # An explicit live continuation retries unfinished calls, but retains
        # policy denials so the same forbidden endpoint is not retried.
        if (
            match
            and self.client is not None
            and not match.get("success")
            and str(match.get("error_code", "")).upper() != "POLICY_DENIED"
            and "POLICY_DENIED" not in (match.get("error") or "")
        ):
            match = None
        code, status = None, None
        if match:
            result, error, ec = match.get("result"), match.get("error"), match.get("ec")
            started = match.get("fetched_at", started)
            code, status = match.get("error_code"), match.get("http_status")
            if error and (str(code).upper() == "POLICY_DENIED" or "POLICY_DENIED" in error):
                self.blocked_endpoints[endpoint] = error
            elif error and error.startswith(("AuthError:", "QuotaError:")):
                self.stop_reason = error
        elif endpoint in self.blocked_endpoints:
            return None
        elif self.client is None:
            error = "No matching recorded call"
        else:
            try:
                self.client.last_ec_cost = None
                method = self.client
                for part in endpoint.split("."):
                    method = getattr(method, part)
                result = method(**params)
                ec = getattr(self.client, "last_ec_cost", None)
                if endpoint in self.policy_retried:
                    self.policy_cleared.add(endpoint)
            except Exception as exc:
                # Some SDK errors can include request parameters; credentials are never parameters.
                error = f"{type(exc).__name__}: {exc}"
                ec = getattr(self.client, "last_ec_cost", None)
                code, status = getattr(exc, "code", None), getattr(exc, "status", None)
                for secret in (
                    os.environ.get("WMSHARE_CLIENT_ID"),
                    os.environ.get("WMSHARE_CLIENT_SECRET"),
                ):
                    if secret:
                        error = error.replace(secret, "[redacted]")
                if str(code).upper() == "POLICY_DENIED" or "POLICY_DENIED" in error:
                    self.blocked_endpoints[endpoint] = error
                elif type(exc).__name__ in ("AuthError", "QuotaError"):
                    self.stop_reason = error
        record = {
            "endpoint": endpoint,
            "params": params,
            "fetched_at": started,
            "elapsed_seconds": round(time.monotonic() - start, 6),
            "success": error is None,
            "error": error,
            "error_code": code,
            "http_status": status,
            "ec": ec,
            "reused": match is not None,
            "result": result,
        }
        self.records.append(record)
        (self.output / "raw").mkdir(parents=True, exist_ok=True)
        (self.output / "raw" / f"{len(self.records):03d}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return result


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )


def run_probe(
    output: Path,
    database: Path | None,
    as_of: date,
    client=None,
    replay: Path | None = None,
    mode=None,
    resume: Path | None = None,
    experiments=None,
    samples=None,
    retry_policy=False,
):
    try:
        from tools.probe_wmshare_local import load_local_context
    except ModuleNotFoundError:
        from probe_wmshare_local import load_local_context
    output = Path(output)
    replay_dir = Path(replay or resume) if replay or resume else None
    if replay_dir and replay_dir.name == "raw":
        replay_dir = replay_dir.parent
    manifest_path = replay_dir / "run.json" if replay_dir and replay_dir.is_dir() else None
    source_run = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path and manifest_path.exists()
        else {}
    )
    if replay:
        experiments = experiments if experiments is not None else source_run.get("experiments")
        samples = samples if samples is not None else source_run.get("samples")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    selected = set(
        experiments if experiments is not None else ("discovery", "industry", "dividends")
    )
    if not selected <= {"discovery", "industry", "dividends"}:
        raise ValueError("Unknown experiment")
    requested_samples = samples is not None
    samples = [ticker(s) for s in (samples if requested_samples else FIXED)]
    if any(not s for s in samples):
        raise ValueError("Invalid A-share sample ticker")
    samples = list(dict.fromkeys(samples))
    rec = Recorder(client, output, replay, resume, retry_policy=retry_policy)
    cached_directory = next(
        (
            r.get("result")
            for r in rec.source
            if r.get("endpoint") == "stocks.list" and r.get("success")
        ),
        [],
    )
    if not cached_directory:
        cached_directory = list((source_run.get("sample_directory") or {}).values())
    directory = (
        (rec.call("stocks.list") or []) if "discovery" in selected else (cached_directory or [])
    )
    stocks = {
        ticker(r.get("symbol")): r for r in directory if isinstance(r, dict) and is_a_share(r)
    }
    saved_local = source_run.get("local_context") if database is None else None
    pool = saved_local or load_local_context(database, as_of, [])
    universe_raw, watchlist_raw = pool.get("universe_tickers"), pool.get("watchlist_tickers")
    universe, watchlist = set(universe_raw or []), set(watchlist_raw or [])

    def membership(symbol):
        u = symbol in universe if universe_raw is not None else None
        w = symbol in watchlist if watchlist_raw is not None else None
        return u, w, (not u and not w) if u is not None and w is not None else None

    discovery, candidate_lists, counts = [], [], []
    for label, where, order, desc in SCREENS if "discovery" in selected else ():
        result = rec.call(
            "screener.query", where=where, order=order, desc=desc, limit=50, fields=FIELDS
        )
        rows = result.get("rows", []) if isinstance(result, dict) else []
        valid = []
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            symbol = ticker(row.get("symbol") or row.get("$SYMBOL"))
            if symbol not in stocks:
                continue
            valid.append(symbol)
            u, w, new = membership(symbol)
            discovery.append(
                {
                    "screen": label,
                    "symbol": symbol,
                    "name": row.get("name") or row.get("$NAME"),
                    "in_universe": u,
                    "in_watchlist": w,
                    "new_discovery": new,
                    "screen_check": screen_check(label, row),
                    **{k: row.get(k) for k in FIELDS},
                }
            )
        counts.append(
            (
                label,
                result.get("count") if isinstance(result, dict) else None,
                len(rows),
                len(valid),
            )
        )
        candidate_lists.append(valid)
    # Alternate both ranked lists, first outside existing pools and then remaining candidates.
    for outside_only in (True, False) if not requested_samples else ():
        queues = [
            [s for s in seq if not outside_only or membership(s)[2] is True]
            for seq in candidate_lists
        ]
        while any(queues) and len(samples) < 20:
            for q in queues:
                if q:
                    s = q.pop(0)
                    if s not in samples and len(samples) < 20:
                        samples.append(s)
    local = saved_local or load_local_context(database, as_of, samples)
    try:
        from importlib.metadata import version

        sdk_version = version("wmshare")
    except Exception:
        sdk_version = None
    manifest = {
        "mode": "replay" if replay else (mode or "injected"),
        "source_mode": source_run.get("source_mode", source_run.get("mode"))
        if replay
        else (mode or "injected"),
        "as_of": as_of.isoformat(),
        "captured_on": source_run.get("captured_on", as_of.isoformat())
        if replay or resume
        else datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
        "sdk_version": source_run.get("sdk_version") if replay else sdk_version,
        "local_context": local,
        "resumed_from": str(resume) if resume else None,
        "experiments": sorted(selected),
        "samples": samples,
        "sample_directory": {s: stocks[s] for s in samples if s in stocks},
    }
    (output / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    ind_keys = (
        (rec.call("index.universe_keys", group="industry") or []) if "industry" in selected else []
    )
    broad_keys = (
        (rec.call("index.universe_keys", group="broad") or []) if "industry" in selected else []
    )
    catalog = (rec.call("stocks.industries", level=1) or []) if "industry" in selected else []
    # universe_keys returns strings; stocks.industries supplies their names.
    key_by_name = {}
    for r in catalog:
        if isinstance(r, dict):
            key = r.get("symbol") or r.get("key")
            if key and not str(key).startswith("industry."):
                key = "industry." + str(key)
            if key in ind_keys:
                key_by_name[r.get("name")] = key
    if "index.hs300" in broad_keys:
        key_by_name["沪深300"] = "index.hs300"
    trends, industry = {}, []
    for name in NAMES if "industry" in selected else ():
        key = key_by_name.get(name)
        trend = rec.call("index.trend", key=key, window="5y") if key else None
        trend = trend if isinstance(trend, dict) else {}
        pts = sorted(
            (p for p in trend.get("points", []) if isinstance(p, dict) and p.get("date")),
            key=lambda p: p["date"],
        )
        trends[name] = pts
        last = pts[-1] if pts else {}
        # Only compare values in the same actual window, and do not label truncated coverage five years.
        window_start = str(trend.get("win_start") or "")[:10]
        try:
            trend_date = date.fromisoformat(str(trend.get("as_of") or pts[-1]["date"])[:10])
            cutoff = f"{trend_date.year - 5}{trend_date.isoformat()[4:]}"
        except (ValueError, IndexError, KeyError):
            cutoff = ""
        window_pts = [p for p in pts if not cutoff or str(p["date"])[:10] >= cutoff]
        trends[name] = window_pts
        truncated = trend.get("win_trunc")
        problems = []
        if not key:
            problems.append("目录未匹配")
        elif not pts or trend.get("ok") is False:
            problems.append("趋势缺失或调用失败")
        else:
            if not trend.get("as_of"):
                problems.append("服务端数据日期未知")
            if truncated or (cutoff and str(pts[0]["date"])[:10] > cutoff):
                problems.append("窗口截断")
                truncated = True
            for field in ("pe", "pb"):
                supplied = number(trend.get(f"{field}_pct"))
                computed = rank_percentile(window_pts, field) if not truncated else None
                if supplied is None or computed is None:
                    if not trend.get(f"{field}_missing"):
                        problems.append(f"{field.upper()} 分位不足以核对")
                elif abs(supplied - computed) > 0.000001:
                    problems.append(f"{field.upper()} 分位与复算不符")
        row = {
            "name": name,
            "key": key,
            "as_of": trend.get("as_of"),
            "latest_date": last.get("date"),
            "pe": last.get("pe"),
            "pb": last.get("pb"),
            "pe_pct_5y": trend.get("pe_pct") if not truncated else None,
            "pb_pct_5y": trend.get("pb_pct") if not truncated else None,
            "pe_recomputed": rank_percentile(window_pts, "pe") if not truncated else None,
            "pb_recomputed": rank_percentile(window_pts, "pb") if not truncated else None,
            "coverage_start": pts[0].get("date") if pts else None,
            "coverage_end": last.get("date"),
            "window_start": window_start,
            "win_trunc": truncated,
            "pct": trend.get("pct"),
            "missing_reason": "；".join(problems),
        }
        industry.append(row)
    begin = as_of - timedelta(days=1825)
    dividends = []
    for symbol in samples if "dividends" in selected else ():
        metric = rec.call("indicator.metrics", symbol=symbol, codes=CODES) or {}
        metric = metric if isinstance(metric, dict) else {}
        snapshot = rec.call("dividend.snapshot", sym=symbol)
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        y = snapshot.get("yield_ttm")
        payout = snapshot.get("payout_ttm")
        if y is None:
            y = rec.call("dividend.yield_ttm", symbol=symbol)
        if payout is None:
            payout = rec.call("dividend.payout_ttm", symbol=symbol)
        history = rec.call(
            "dividend.history", symbol=symbol, begin=begin.isoformat(), end=as_of.isoformat()
        )
        # The ranged SDK call filters out undated proposals; request the recent list separately.
        recent_history = rec.call("dividend.history", symbol=symbol)
        bar = rec.call("kline.last", symbol=symbol)
        bar = bar if isinstance(bar, dict) else {}
        try:
            event_end = date.fromisoformat(str(snapshot.get("quote_date") or as_of)[:10])
        except ValueError:
            event_end = as_of
        years, recent, proposals, actions = (
            dividend_events(history, begin, event_end)
            if isinstance(history, list)
            else ({}, None, [], [])
        )
        if isinstance(recent_history, list):
            _, _, extra_proposals, _ = dividend_events(recent_history, begin, event_end)
            proposals = extra_proposals
        if not isinstance(history, list):
            history_status = "unknown/failed"
        elif not history or not years:
            history_status = "empty/unconfirmed"
            years, recent = None, None
        elif any(value is None for value in years.values()):
            history_status = "missing_amounts"
        else:
            history_status = "available"
        loc = local.get("stocks", {}).get(symbol, {}) or {}
        local_y = number(loc.get("dividend_yield_ttm"))
        y_num = number(y)
        bar_date = bar.get("date") or bar.get("kline_date")
        same_date = bool(
            as_of.isoformat() == manifest["captured_on"]
            and bar_date
            and loc.get("price_date")
            and str(bar_date)[:10] == str(loc["price_date"])[:10]
            and snapshot.get("quote_date")
            and str(snapshot["quote_date"])[:10] == str(bar_date)[:10]
        )
        price = number(bar.get("close"))
        event_yield = (
            recent / price if recent is not None and price is not None and price > 0 else None
        )
        industry_name = str(
            stocks.get(symbol, {}).get("industry_name") or metric.get("$INDUSTRY_CUR") or ""
        )
        industry_key = str(stocks.get(symbol, {}).get("industry") or "")
        mapped = next(
            (
                n
                for n in NAMES[:-1]
                if (key_by_name.get(n) and industry_key.startswith(key_by_name[n]))
                or n in industry_name
            ),
            None,
        )
        comparisons = []
        for d in discovery:
            if d["symbol"] == symbol:
                for field in FIELDS:
                    if d.get(field) is not None and metric.get(field) is None:
                        comparisons.append(f"{d['screen']} {field}: 单股查询缺失")
                    elif d.get(field) is not None and d[field] != metric.get(field):
                        comparisons.append(f"{d['screen']} {field}: {d[field]} vs {metric[field]}")
        dividends.append(
            {
                "symbol": symbol,
                "name": stocks.get(symbol, {}).get("name"),
                "industry": industry_name,
                "industry_background": mapped,
                "in_universe": membership(symbol)[0],
                "in_watchlist": membership(symbol)[1],
                "yield_ttm": y,
                "snapshot_quote_date": snapshot.get("quote_date"),
                "snapshot_dps_ttm": snapshot.get("dps_ttm"),
                "snapshot_bonus_ttm": snapshot.get("bonus_ttm"),
                "snapshot_market_value": snapshot.get("mv"),
                "yield_3y_avg": metric.get("$BONUS_RATE_3Y_AVG_LAST")
                if metric.get("$BONUS_RATE_3Y_AVG_LAST") is not None
                else snapshot.get("yield_3y_avg"),
                "payout_ttm": payout,
                "roe_ttm": metric.get("$ROE_TTM_LAST"),
                "cashflow_to_profit": metric.get("$NCFO_NP"),
                "history_begin": begin.isoformat(),
                "history_end": as_of.isoformat(),
                "event_window_end": event_end.isoformat(),
                "history_status": history_status,
                "annual_dps": years if isinstance(history, list) else None,
                "recent_365d_dps": recent,
                "event_yield_ttm": event_yield,
                "snapshot_event_yield_difference": y_num - event_yield
                if y_num is not None
                and event_yield is not None
                and snapshot.get("quote_date")
                and str(snapshot["quote_date"])[:10] == str(bar_date)[:10]
                else None,
                "proposals": proposals,
                "share_actions": actions,
                "price": bar.get("close"),
                "price_date": bar_date,
                "local_price": loc.get("price"),
                "local_price_date": loc.get("price_date"),
                "local_yield_ttm": local_y,
                "yield_difference_same_date": y_num - local_y
                if y_num is not None and local_y is not None and same_date
                else None,
                "local_annual_roe": loc.get("annual_roe"),
                "local_report_period": loc.get("report_period"),
                "local_dividend_events": loc.get("dividend_events"),
                "local_dividend_coverage": loc.get("dividend_coverage"),
                "local_note": loc.get("note"),
                "screen_metric_differences": comparisons,
            }
        )
    write_csv(
        output / "discovery.csv",
        discovery,
        ["screen", "symbol", "name", "in_universe", "in_watchlist", "new_discovery", "screen_check"]
        + FIELDS,
    )
    write_csv(
        output / "industry.csv",
        industry,
        list(industry[0])
        if industry
        else [
            "name",
            "key",
            "as_of",
            "latest_date",
            "pe",
            "pb",
            "pe_pct_5y",
            "pb_pct_5y",
            "pe_recomputed",
            "pb_recomputed",
            "coverage_start",
            "coverage_end",
            "window_start",
            "win_trunc",
            "pct",
            "missing_reason",
        ],
    )
    write_csv(
        output / "dividends.csv",
        dividends,
        list(dividends[0])
        if dividends
        else [
            "symbol",
            "name",
            "industry",
            "industry_background",
            "in_universe",
            "in_watchlist",
            "yield_ttm",
            "snapshot_quote_date",
            "snapshot_dps_ttm",
            "snapshot_bonus_ttm",
            "snapshot_market_value",
            "yield_3y_avg",
            "payout_ttm",
            "roe_ttm",
            "cashflow_to_profit",
            "history_begin",
            "history_end",
            "event_window_end",
            "history_status",
            "annual_dps",
            "recent_365d_dps",
            "event_yield_ttm",
            "snapshot_event_yield_difference",
            "proposals",
            "share_actions",
            "price",
            "price_date",
            "local_price",
            "local_price_date",
            "local_yield_ttm",
            "yield_difference_same_date",
            "local_annual_roe",
            "local_report_period",
            "local_dividend_events",
            "local_dividend_coverage",
            "local_note",
            "screen_metric_differences",
        ],
    )
    chart_note = ""
    if "industry" in selected:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(2, 1, figsize=(11, 8), sharex=True)
            fig.suptitle(
                "wmshare: five-year industry valuation"
                + (" (offline fixture)" if manifest["source_mode"] != "live" else "")
            )
            for name, pts in trends.items():
                for ax, field in zip(axes, ("pe", "pb"), strict=True):
                    xy = [
                        (p["date"], p.get(field)) for p in pts if number(p.get(field)) is not None
                    ]
                    if xy:
                        ax.plot(
                            [date.fromisoformat(str(x)[:10]) for x, _ in xy],
                            [y for _, y in xy],
                            label=dict(
                                zip(
                                    NAMES,
                                    (
                                        "Banks",
                                        "Food & Beverage",
                                        "Utilities",
                                        "Electronics",
                                        "CSI 300",
                                    ),
                                    strict=True,
                                )
                            )[name],
                        )
            for ax, field in zip(axes, ("PE", "PB"), strict=True):
                ax.set_ylabel(field)
                if ax.lines:
                    ax.legend(fontsize="small")
            fig.autofmt_xdate()
            fig.tight_layout()
            fig.savefig(output / "industry-trend.png")
            plt.close(fig)
        except ImportError:
            chart_note = "图表未生成：未安装 matplotlib。"
    failures = [r for r in rec.records if not r["success"]]
    known_ec = [r["ec"] for r in rec.records if number(r["ec"]) is not None]
    reused = sum(r.get("reused", False) for r in rec.records)
    new_ec = sum(
        r["ec"] for r in rec.records if not r.get("reused") and number(r["ec"]) is not None
    )
    top = sorted(
        (r for r in dividends if number(r["yield_ttm"]) is not None),
        key=lambda r: r["yield_ttm"],
        reverse=True,
    )[:5]
    new_by_symbol = {}
    for row in discovery:
        symbol = row["symbol"]
        if symbol in samples and symbol not in FIXED and row["new_discovery"]:
            entry = new_by_symbol.setdefault(symbol, {**row, "matched_screens": []})
            if row["screen"] not in entry["matched_screens"]:
                entry["matched_screens"].append(row["screen"])
    new = list(new_by_symbol.values())
    discovery_verdict = (
        "补查证券目录或筛选接口失败"
        if not stocks or any(c[1] is None for c in counts)
        else "补查筛选行是否符合条件"
        if any(r["screen_check"] != "supported" for r in discovery)
        else "补查筛选与单股指标差异"
        if any(r["screen_metric_differences"] for r in dividends)
        else "补查样本筛选或单股指标缺失"
        if any(any(r.get(f) is None for f in FIELDS) for r in discovery)
        or any(
            r["endpoint"] == "indicator.metrics" and (not r["success"] or not r["result"])
            for r in rec.records
        )
        else "补查本地池归属"
        if universe_raw is None or watchlist_raw is None
        else "值得接入候选发现流程，继续研究池外线索"
        if new
        else "暂不采用：前 50 行未发现可确认的池外样本"
    )
    industry_verdict = (
        "补查行业目录、趋势覆盖和分位差异"
        if any(r["missing_reason"] for r in industry)
        else "值得接入股票页或行业页，持续观察更新日期"
    )
    cash_differences = [
        r
        for r in dividends
        if number(r.get("snapshot_event_yield_difference")) is not None
        and abs(r["snapshot_event_yield_difference"]) > 0.000001
    ]
    dividend_verdict = (
        f"{len(cash_differences)} 只平台 TTM 与已实施现金股息不同，先按各自口径展示"
        if cash_differences
        else "补查股息快照或事件历史缺失"
        if not top
        or any(r["history_status"] != "available" or r["payout_ttm"] is None for r in dividends)
        else "值得扩展股票页：可展示事件连续性与派息率"
    )
    if manifest["source_mode"] != "live":
        discovery_verdict = "离线流程已验证，真实数据价值待取数；流程诊断：" + discovery_verdict
        industry_verdict = "离线流程已验证，真实数据价值待取数；流程诊断：" + industry_verdict
        dividend_verdict = "离线流程已验证，真实数据价值待取数；流程诊断：" + dividend_verdict
    if "discovery" not in selected:
        discovery_verdict = "未选择实验"
    if "industry" not in selected:
        industry_verdict = "未选择实验"
    if "dividends" not in selected:
        dividend_verdict = "未选择实验"
    lines = [
        f"# wmshare 实验报告 — {as_of}",
        "",
        f"运行模式：{'回放' if replay else '真实取数' if manifest['mode'] == 'live' else '注入客户端（可能为合成数据）'}；来源模式：{manifest['source_mode'] or '未知'}。",
        f"本地对照：{local.get('status', 'unknown')}；{local.get('note', '')}。as-of 只限定分红事件窗口，筛选、快照及最新价格是抓取时的最新值，并非历史回溯。",
        f"样本 {len(samples)} 只：{', '.join(samples)}。抓取日期与服务端数据日期可能不同；无日期记为未知。",
        "",
        "## 候选发现",
        f"结论：{discovery_verdict}。",
        "",
    ]
    for label, count, returned, valid in counts:
        lines.append(
            f"- {label}: 服务端命中 {count if count is not None else '未知'}；返回 {returned} 行，其中有效 A 股 {valid} 行（不是完整命中名单）。"
        )
    for r in new[:8]:
        lines.append(
            f"- {r['symbol']} {r['name'] or ''}，行业 {r.get('$INDUSTRY_CUR') or '未知'}，命中 {', '.join(r['matched_screens'])}；池外。研究问题：核对盈利质量、现金流与估值持续性。"
        )
    lines.append(
        f"筛选条件核对：符合 {sum(r['screen_check'] == 'supported' for r in discovery)} 行，矛盾 {sum(r['screen_check'] == 'contradiction' for r in discovery)} 行，字段不足 {sum(r['screen_check'] == 'unknown' for r in discovery)} 行。"
    )
    diffs = [r for r in dividends if r["screen_metric_differences"]]
    lines += [
        f"筛选与单股指标有差异的样本：{len(diffs)}；详情见 dividends.csv。年度 ROE 与 TTM ROE 分列，不计算误差。",
        "",
        "## 行业估值背景",
        f"结论：{industry_verdict}。",
        "银行主要观察 PB；行业整体法与个股估值分位口径不同。",
    ]
    for r in industry:
        lines.append(
            f"- {r['name']} ({r['key'] or '未匹配'})：最新 {r['latest_date'] or '未知'}，PE {r['pe']}，PB {r['pb']}；覆盖 {r['coverage_start']} 至 {r['coverage_end']}；五年 PE/PB 分位 {r['pe_pct_5y']}/{r['pb_pct_5y']}，复算 {r['pe_recomputed']}/{r['pb_recomputed']}；{r['missing_reason']}"
        )
    lines += [
        chart_note,
        "",
        "## 高股息研究",
        f"结论：{dividend_verdict}。",
        f"历史请求窗口 {begin} 至 {as_of}；现金股息统计按各快照 quote_date 的 (日期−365天, 日期] 对齐。首尾年度为部分年度，历史查询失败或未知不视为零。预案与未确认实施的记录单列；每股分红未做公司行动复权。",
    ]
    for r in top:
        diff = r["yield_difference_same_date"]
        judgment = dividend_judgment(r, begin, as_of)
        lines.append(
            f"- {r['symbol']}（{judgment}）：TTM 股息率 {r['yield_ttm']}，派息率 {r['payout_ttm']}，现金流/净利润 {r['cashflow_to_profit']}，年度 DPS {r['annual_dps']}，历史状态 {r['history_status']}；本地同日差 {diff if diff is not None else '未知（日期或数值不匹配）'}。"
            + (
                " 差异超过 0.3 个百分点：核查税前税后、预案/实施及价格口径。"
                if diff is not None and abs(diff) > 0.003
                else " 核查税前税后、预案/实施和价格口径。"
            )
        )
    lines += [
        "",
        "## 调用与缺口",
        f"{'回放历史调用记录（并非本次计费）' if replay else '调用'} {len(rec.records)} 次，失败 {len(failures)} 次；已知 EC 合计 {sum(known_ec)}，未知 {len(rec.records) - len(known_ec)} 次。未知不计为零；实际周期成本待真实记录估算。",
        f"其中复用已有记录 {reused} 条，本次新增调用 {len(rec.records) - reused} 次；本次新增调用已知 EC {new_ec}。",
        "周期参考：上述两条筛选每周运行一次，五个行业/指数趋势每日更新；20 只样本的分红及相关指标每周更新。按 raw/ 中对应调用累加即可估算这些固定规模任务的成本，不外推全市场逐票成本。",
    ]
    lines += [f"- {r['endpoint']} {r['params']}: {r['error']}" for r in failures]
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "samples": samples,
        "records": rec.records,
        "discovery": discovery,
        "industry": industry,
        "dividends": dividends,
    }


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument(
        "--replay", type=Path, help="Recorded run directory or raw records; no network"
    )
    inputs.add_argument(
        "--resume", type=Path, help="Reuse recorded calls and fetch only missing calls"
    )
    parser.add_argument("--experiments", nargs="+", choices=("discovery", "dividends", "industry"))
    parser.add_argument(
        "--samples", nargs="+", help="Limit detailed samples to these A-share tickers"
    )
    parser.add_argument(
        "--retry-policy",
        action="store_true",
        help="Retry a prior policy denial once per endpoint after authorization changes",
    )
    args = parser.parse_args(argv)
    if args.as_of is None and (args.replay or args.resume):
        source = args.replay or args.resume
        replay_dir = source.parent if source.name == "raw" else source
        manifest_path = replay_dir / "run.json"
        if manifest_path.is_file():
            args.as_of = date.fromisoformat(
                json.loads(manifest_path.read_text(encoding="utf-8"))["as_of"]
            )
    args.as_of = args.as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    injected = client is not None
    if client is None and args.replay is None:
        missing = [
            name
            for name in ("WMSHARE_CLIENT_ID", "WMSHARE_CLIENT_SECRET")
            if not os.environ.get(name)
        ]
        if missing:
            print("Missing required environment variables: " + ", ".join(missing), file=sys.stderr)
            return 2
        try:
            from wmshare import Client
        except ImportError:
            print("Install the base SDK: pip install wmshare==1.19.0", file=sys.stderr)
            return 2
        options = {
            "client_id": os.environ["WMSHARE_CLIENT_ID"],
            "client_secret": os.environ["WMSHARE_CLIENT_SECRET"],
        }
        if os.environ.get("WMSHARE_BASE_URL"):
            options["base_url"] = os.environ["WMSHARE_BASE_URL"]
        client = Client(**options)
    try:
        result = run_probe(
            args.output,
            args.database,
            args.as_of,
            client=client,
            replay=args.replay,
            mode="injected" if injected else "live",
            resume=args.resume,
            experiments=args.experiments,
            samples=args.samples,
            retry_policy=args.retry_policy,
        )
        manifest_path = args.output / "run.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if injected and not args.replay:
            manifest["mode"] = "injected"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failures = sum(not record["success"] for record in result["records"])
    print(
        f"Saved {args.output / 'summary.md'}; {len(result['samples'])} samples, {failures} failed calls."
    )
    return 1 if result["records"] and all(not r["success"] for r in result["records"]) else 0


if __name__ == "__main__":
    sys.exit(main())
