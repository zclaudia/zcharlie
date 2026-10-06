"""iFinD ``financial_snapshot`` (watchlist scope): one period's canonical fields.

Target pools are collected from baostock (:mod:`investment_agent.snapshots.breadth`,
decision D6); this collector serves ``provider="ifind"``, ``scope="watchlist"``
payloads only.

The request plan and the field extraction both come from the auditable
registry in :mod:`investment_agent.research.ifind_mapping` - there is no
second mapping. For each universe member without any capture of the period
the collector calls every ``/financial/...`` endpoint the registry cites
(three tickers per call) and stores:

* every *direct* financial field (unit conversion from the registry:
  "provider percent ÷ 100" -> fraction, otherwise the plain number);
* single-period derived fields (free cash flow);
* ``source_values``: the raw registry source fields seen in the responses
  (e.g. per-share EPS / NAV / revenue behind PE / PB / PS).

Multi-period derived fields (growth, ROE stability) need several periods and
are left to the factor library.

An answer with no values for a ticker means "not yet disclosed": the ticker
is recorded as ``pending`` (not an error) and retried by the next run. Live
rows use ``availability="approx"`` with ``available_from`` = the capture date
in Asia/Shanghai - a conservative upper bound on the public date until the
disclosure-date collector (P1-A6) refines it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import structlog

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.research.ifind_chain import IfindProxyResult
from investment_agent.research.ifind_mapping import IFIND_FIELD_MAPPINGS, IfindFieldMapping
from investment_agent.snapshots.collect import (
    IFIND_PROVIDER,
    ArtifactWriter,
    CollectionReport,
    Completeness,
    ProxyBatchCaller,
    chunked,
    error_reason,
    fence_writes,
    rows_by_ticker,
)
from investment_agent.snapshots.jobs import FinancialSnapshotPayload, period_kind
from investment_agent.snapshots.scope import scope_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, RunLease

log = structlog.get_logger(__name__)

_PERCENT_MARKER = "percent"


@dataclass(frozen=True)
class FinancialEndpoint:
    """One registry endpoint such as ``/financial/index:profitability``."""

    key: str
    path: str
    selector_name: str
    selector_value: str

    def payload(self, tickers: list[str], report_period: date) -> dict[str, Any]:
        return {
            "ticker": ",".join(tickers),
            "report_date": report_period.strftime("%Y%m%d"),
            self.selector_name: self.selector_value,
        }


def _endpoint_parts(endpoint: str) -> list[str]:
    return [part.strip() for part in endpoint.split("+") if part.strip()]


def _parse_endpoint(key: str) -> FinancialEndpoint:
    path, _, selector = key.partition(":")
    if not selector:
        raise ValueError(f"financial endpoint {key!r} lacks a statement/category selector")
    name = "statement" if path == "/financial/statements" else "category"
    return FinancialEndpoint(key=key, path=path, selector_name=name, selector_value=selector)


def financial_endpoints(
    mappings: tuple[IfindFieldMapping, ...] = IFIND_FIELD_MAPPINGS,
) -> list[FinancialEndpoint]:
    """Every ``/financial/...`` endpoint the registry cites, in registry order."""

    keys: list[str] = []
    for mapping in mappings:
        for part in _endpoint_parts(mapping.endpoint):
            if part.startswith("/financial/") and part not in keys:
                keys.append(part)
    return [_parse_endpoint(key) for key in keys]


def _source_fields_by_endpoint(
    mappings: tuple[IfindFieldMapping, ...],
) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    for mapping in mappings:
        for part in _endpoint_parts(mapping.endpoint):
            if part.startswith("/financial/"):
                fields.setdefault(part, set()).update(mapping.source_fields)
    return fields


def _number(value: object) -> float | None:
    text = str(value if value is not None else "").strip().replace(",", "")
    if not text or text in {"--", "None", "null", "nan"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _convert(mapping: IfindFieldMapping, raw: object) -> float | None:
    number = _number(raw)
    if number is None:
        return None
    if _PERCENT_MARKER in mapping.unit_or_formula:
        return number / 100.0
    return number


def _free_cash_flow(values: dict[str, float | None], mapping: IfindFieldMapping) -> float | None:
    operating, capex = (values.get(field) for field in mapping.source_fields)
    if operating is None or capex is None:
        return None
    return operating - capex


# Single-period derived financial fields computable from one report period.
_SINGLE_PERIOD_DERIVED: dict[
    str, Callable[[dict[str, float | None], IfindFieldMapping], float | None]
] = {"free_cash_flow": _free_cash_flow}


def canonical_financial_fields(
    rows: dict[str, dict[str, str]],
    mappings: tuple[IfindFieldMapping, ...] = IFIND_FIELD_MAPPINGS,
) -> dict[str, object] | None:
    """Build one listing's ``fields`` from its row per endpoint key.

    Returns ``None`` when no tracked source field has a value (not yet
    disclosed).
    """

    tracked = _source_fields_by_endpoint(mappings)
    source_values: dict[str, float | None] = {}
    for key, row in rows.items():
        for source_field in sorted(tracked.get(key, set())):
            if source_field in row:
                source_values[source_field] = _number(row.get(source_field))
    if not any(value is not None for value in source_values.values()):
        return None

    fields: dict[str, object] = {}
    skipped: list[str] = []
    for mapping in mappings:
        if mapping.payload_type != "financial":
            continue
        name = mapping.canonical_field
        if mapping.status == "direct":
            (endpoint,) = _endpoint_parts(mapping.endpoint)
            row = rows.get(endpoint, {})
            (source_field,) = mapping.source_fields
            fields[name] = _convert(mapping, row.get(source_field))
        elif name in _SINGLE_PERIOD_DERIVED:
            fields[name] = _SINGLE_PERIOD_DERIVED[name](source_values, mapping)
        else:
            skipped.append(name)
    fields["missing_fields"] = sorted(
        name for name, value in fields.items() if name != "missing_fields" and value is None
    )
    fields["multi_period_fields"] = sorted(skipped)
    fields["source_values"] = dict(sorted(source_values.items()))
    return fields


async def collect_financial_snapshot(
    payload: FinancialSnapshotPayload,
    *,
    caller: ProxyBatchCaller,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
    now: Callable[[], datetime],
) -> CollectionReport:
    if payload.provider != IFIND_PROVIDER:
        raise ValueError(f"iFinD financial collector got provider {payload.provider!r}")
    report_period = payload.report_period
    kind = period_kind(report_period)
    async with session_scope() as session:
        listing_by_ticker = await scope_members(session, payload, payload.as_of)
        present = await FinancialSnapshotRepository(session).listings_with_period(
            list(listing_by_ticker.values()), report_period, provider=IFIND_PROVIDER
        )

    completeness = Completeness(expected=sorted(listing_by_ticker))
    report = CollectionReport(completeness)
    todo: list[str] = []
    for ticker, listing_id in sorted(listing_by_ticker.items()):
        if listing_id in present:
            completeness.mark_collected(ticker)
        else:
            todo.append(ticker)

    endpoints = financial_endpoints()
    stored = 0
    for index, batch in enumerate(chunked(todo), start=1):
        try:
            results: dict[str, IfindProxyResult] = {}
            for endpoint in endpoints:
                results[endpoint.key] = await caller.call(
                    endpoint.path, endpoint.payload(batch, report_period)
                )
            ref = artifacts.write(
                f"financial-{report_period.isoformat()}-b{index:03d}",
                {
                    key: {
                        "path": endpoint.path,
                        "request": endpoint.payload(batch, report_period),
                        "body": results[key].body,
                    }
                    for endpoint in endpoints
                    for key in (endpoint.key,)
                },
            )
            grouped = {key: rows_by_ticker(result.rows) for key, result in results.items()}
            captures: dict[str, dict[str, object]] = {}
            for ticker in batch:
                ticker_rows = {
                    key: rows[ticker][0] for key, rows in grouped.items() if rows.get(ticker)
                }
                fields = canonical_financial_fields(ticker_rows)
                if fields is None:
                    completeness.mark_missing(
                        ticker, "pending", f"{report_period.isoformat()} not yet disclosed"
                    )
                    continue
                captures[ticker] = fields
            captured_at = now()
            available_from = captured_at.astimezone(SHANGHAI_TZ).date()
            async with session_scope() as session:
                await fence_writes(session, lease)
                repository = FinancialSnapshotRepository(session)
                for ticker, fields in captures.items():
                    await repository.insert(
                        listing_id=listing_by_ticker[ticker],
                        report_period=report_period,
                        period_kind=kind,
                        fields=fields,
                        provider=IFIND_PROVIDER,
                        captured_at=captured_at,
                        availability="approx",
                        available_from=available_from,
                        raw_ref=ref,
                        is_backfill=False,
                    )
            for ticker in captures:
                completeness.mark_collected(ticker)
            stored += len(captures)
        except LeaseLostError:
            raise
        except Exception as exc:
            reason = error_reason(exc)
            report.record_failure(exc)
            log.warning("snapshot.financial.batch_failed", tickers=batch, error=reason)
            for ticker in batch:
                completeness.mark_missing(ticker, "failed", reason)

    report.metadata = {
        "provider": IFIND_PROVIDER,
        "scope": payload.scope,
        "universe_code": payload.universe_code,
        "report_period": report_period.isoformat(),
        "period_kind": kind,
        "as_of": payload.as_of.isoformat(),
        "endpoints": [endpoint.key for endpoint in endpoints],
        "already_present": len(present),
        "requested": len(todo),
        "stored": stored,
        "proxy_calls": caller.calls,
        "availability": "approx: available_from = capture date (Asia/Shanghai)",
    }
    return report
