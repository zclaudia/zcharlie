"""End-to-end iFinD research validation workflow.

This module intentionally keeps the provider adapter separate from the deterministic
scoring runtime.  iFinD supplies facts, the repository-owned kernel computes scores,
and the LLM explains only the generated score context.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.metrics import MIN_ONE_YEAR_SESSIONS, ONE_YEAR_WINDOW_DAYS
from investment_agent.factors.registry import (
    FLAG_NEGATIVE_BOOK_VALUE,
    FLAG_NEGATIVE_EARNINGS,
    FLAG_NON_POSITIVE_REVENUE,
    FLAG_SHORT_HISTORY,
)
from investment_agent.factors.view import ifind_research_view
from investment_agent.llm.glossary import readability_rules
from investment_agent.llm.openai_compatible import OpenAICompatibleClient
from investment_agent.providers.errors import QuotaExhaustedError
from investment_agent.research.contracts import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalResearchBundle,
)
from investment_agent.research.kernel import (
    DeterministicResearchKernel,
    InProcessResearchKernel,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
)

_TICKER_PATTERN = re.compile(r"^(?P<symbol>\d{6})\.(?P<market>SH|SZ|BJ)$")

# Canonical metric -> factor that defines it on the observation (iFinD) view.
_OBSERVATION_FACTORS: dict[str, str] = {
    "pe": "pe_fy",
    "pb": "pb_fy",
    "ps": "ps_fy",
    "return_1y": "return_1y",
    "volatility_1y": "volatility_1y",
    "max_drawdown_1y": "max_drawdown_1y",
    "market_cap": "market_cap",
    "roe": "roe",
    "roe_stability": "roe_stability",
    "gross_margin": "gross_margin",
    "net_margin": "net_margin",
    "debt_ratio": "debt_ratio",
    "revenue_growth_3y": "revenue_growth_3y",
    "net_profit_growth_3y": "net_profit_growth_3y",
    "free_cash_flow": "free_cash_flow",
}
# (canonical multiple, FY per-share field, factor flag, quality-flag message)
_NOT_MEANINGFUL_MULTIPLES: tuple[tuple[str, str, str, str], ...] = (
    (
        "pe",
        "eps_basic_fy",
        FLAG_NEGATIVE_EARNINGS,
        "PE not meaningful: FY{year} basic EPS {value} <= 0 (loss-making)",
    ),
    (
        "pb",
        "bvps_fy",
        FLAG_NEGATIVE_BOOK_VALUE,
        "PB not meaningful: FY{year} net assets per share {value} <= 0",
    ),
    (
        "ps",
        "revenue_ps_fy",
        FLAG_NON_POSITIVE_REVENUE,
        "PS not meaningful: FY{year} revenue per share {value} <= 0",
    ),
)


@dataclass(frozen=True)
class IfindProxyResult:
    """One decoded proxy response plus its normalized tabular rows."""

    path: str
    body: dict[str, Any]
    rows: list[dict[str, str]]


@dataclass(frozen=True)
class CanonicalBundle:
    """Canonical inputs and the raw provider responses behind them."""

    pool: dict[str, Any]
    market: dict[str, Any]
    financial: dict[str, Any]
    raw: dict[str, Any]


class EvidenceClaim(BaseModel):
    """One agent-written claim with machine-checkable score-field support."""

    text: str
    supporting_fields: list[str] = Field(min_length=1)


class ResearchExplanation(BaseModel):
    """Agent explanation constrained to deterministic score context."""

    summary: str
    classification_rationale: list[EvidenceClaim]
    positive_evidence: list[EvidenceClaim]
    negative_evidence: list[EvidenceClaim]
    risk_factors: list[EvidenceClaim]
    data_quality_notes: list[EvidenceClaim]
    open_questions: list[str]
    workflow_conclusion: str

    @field_validator("workflow_conclusion", mode="before")
    @classmethod
    def _flatten_structured_conclusion(cls, value: Any) -> Any:
        # Models sometimes answer {"status": "needs_review", "warnings": [...]}
        # instead of a sentence; keep every part so the label token survives.
        if isinstance(value, dict):
            return "；".join(
                f"{key}: {item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)}"
                for key, item in value.items()
            )
        return value


@dataclass(frozen=True)
class ResearchChainResult:
    run_id: int
    artifact_dir: Path
    score: dict[str, Any]
    explanation: ResearchExplanation


class ResearchRunConflictError(RuntimeError):
    """Raised when an idempotency key belongs to a non-replayable run."""

    def __init__(self, *, run_id: int, status: str):
        self.run_id = run_id
        self.status = status
        super().__init__(f"research run {run_id} already exists with status={status}")


class ResearchPartialError(RuntimeError):
    """Raised after deterministic results were persisted without an explanation."""

    def __init__(self, *, run_id: int, reason: str):
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"research run {run_id} completed partially: {reason}")


# Upstream quota answers relayed by older ifind-proxy builds as HTTP 502
# (the Kimi gateway's ``403 You've reached your 5-hour usage limit``).
_QUOTA_MARKERS = ("usage limit", "[http 403]")
_MAX_DETAIL_CHARS = 300


def _response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail: object = body.get("detail", body) if isinstance(body, dict) else body
    text = detail if isinstance(detail, str) else (response.text if detail is None else str(detail))
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL_CHARS else text[: _MAX_DETAIL_CHARS - 3] + "..."


def proxy_quota_error(response: httpx.Response, path: str) -> QuotaExhaustedError | None:
    """Classify an ifind-proxy answer as quota exhaustion.

    * HTTP 429 - ifind-proxy maps the gateway's usage-limit error to 429;
    * HTTP 502 whose detail mentions ``usage limit`` or ``[HTTP 403]`` - the
      same condition relayed by proxy builds that predate the 429 mapping.
    """

    status = response.status_code
    if status not in {429, 502}:
        return None
    detail = _response_detail(response)
    if status == 502 and not any(marker in detail.lower() for marker in _QUOTA_MARKERS):
        return None
    return QuotaExhaustedError(
        f"iFinD quota exhausted (HTTP {status} from ifind-proxy at {path}): {detail}; "
        "not retried - collection resumes once the gateway quota window resets",
        provider="ifind",
        status_code=status,
        detail=detail,
    )


ProxyCallHook = Callable[[str, dict[str, Any], str], Awaitable[None]]
"""``(path, payload, outcome)`` after every proxy call; outcome ``ok`` / ``error`` /
``quota``. Used to count calls in the iFinD budget ledger (P1-A13); a hook must
not raise."""


class IfindProxyClient:
    """Small async client that unwraps the proxy's nested ``data_preview`` CSV.

    Quota exhaustion is raised as :class:`QuotaExhaustedError` (terminal, never
    retried); other HTTP errors keep their ``httpx.HTTPStatusError`` form so the
    shared retry policy classifies them.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        on_call: ProxyCallHook | None = None,
    ):
        if not api_key:
            raise ValueError("IFIND_PROXY_API_KEY is required")
        self._on_call = on_call
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> IfindProxyClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        outcome = "error"
        try:
            result = await self._post_rows(path, payload)
            outcome = "ok"
            return result
        except QuotaExhaustedError:
            outcome = "quota"
            raise
        finally:
            if self._on_call is not None:
                await self._on_call(path, payload, outcome)

    async def _post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        response = await self._client.post(path, json=payload)
        quota = proxy_quota_error(response, path)
        if quota is not None:
            raise quota
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"Unexpected iFinD proxy response at {path}: expected object")
        return IfindProxyResult(path=path, body=body, rows=parse_proxy_rows(body))


def parse_proxy_rows(body: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize direct JSON rows or the proxy's JSON-wrapped CSV preview."""

    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("data_preview"), str):
        data = data["data_preview"]

    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        rows = csv.DictReader(io.StringIO(text))
        return [
            {str(key): "" if value is None else str(value).strip() for key, value in row.items()}
            for row in rows
        ]

    if isinstance(data, list):
        return [
            {str(key): "" if value is None else str(value).strip() for key, value in row.items()}
            for row in data
            if isinstance(row, dict)
        ]

    if isinstance(data, dict):
        return [
            {str(key): "" if value is None else str(value).strip() for key, value in data.items()}
        ]

    raise ValueError(
        f"Unsupported iFinD proxy data shape: format={body.get('format')!r}, "
        f"type={type(data).__name__}"
    )


def _first_row(result: IfindProxyResult, name: str) -> dict[str, str]:
    """Return the first row of a latest-period endpoint; fail hard when empty."""

    if not result.rows:
        raise ValueError(f"iFinD returned no rows for {name}")
    return result.rows[0]


def _latest_session(history_rows: list[dict[str, str]]) -> date:
    """Date of the latest parseable session (the default one-year anchor)."""

    dates = []
    for row in history_rows:
        try:
            dates.append(datetime.strptime(str(row.get("time", "")), "%Y%m%d").date())
        except ValueError:
            continue
    if not dates:
        raise ValueError("iFinD returned no dated daily price history")
    return max(dates)


def _source(
    field: str,
    endpoint: str,
    updated_at: str,
    *,
    quality: str = "direct",
    note: str = "",
) -> dict[str, Any]:
    return {
        "field": field,
        "provider": "ifind",
        "source": endpoint,
        "quality": quality,
        "updated_at": updated_at,
        "note": note,
    }


def with_quality(
    item: dict[str, Any],
    required_fields: tuple[str, ...],
    *,
    derived_fields: tuple[str, ...] = (),
    not_meaningful: dict[str, tuple[str, str]] | None = None,
    extra_flags: list[dict[str, Any]] | None = None,
    provider: str = "ifind",
    missing_message: str = "iFinD normalization could not produce {field}",
    derived_message: str = "{field} was derived from iFinD source fields",
    provider_conflicts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach missing-field, derived-field and data-quality audit fields.

    ``not_meaningful`` maps fields that are intentionally ``None`` (e.g. PE of a
    loss-making company) to ``(flag, message)``; they are flagged for review
    instead of being reported as provider data gaps. Shared by every source
    mode (``provider`` and the message templates name the source).
    """

    not_meaningful = not_meaningful or {}
    missing = [
        field
        for field in required_fields
        if item.get(field) is None and field not in not_meaningful
    ]
    flags = [
        {
            "field": field,
            "flag": "missing_field",
            "message": missing_message.format(field=field),
            "provider": provider,
            "severity": "review",
        }
        for field in missing
    ]
    flags.extend(
        {
            "field": field,
            "flag": "derived_field",
            "message": derived_message.format(field=field),
            "provider": provider,
            "severity": "info",
        }
        for field in derived_fields
        if item.get(field) is not None
    )
    flags.extend(
        {
            "field": field,
            "flag": flag,
            "message": message,
            "provider": provider,
            "severity": "review",
        }
        for field, (flag, message) in not_meaningful.items()
        if item.get(field) is None
    )
    flags.extend(extra_flags or [])
    item["missing_fields"] = missing
    item["quality_flags"] = flags
    item["provider_conflicts"] = dict(provider_conflicts or {})
    item["warnings"] = [flag["message"] for flag in flags]
    item["needs_agent_review"] = (
        bool(missing)
        or bool(item["provider_conflicts"])
        or any(flag["severity"] in {"review", "error"} for flag in flags)
    )
    return item


def _quote_timestamp(history_rows: list[dict[str, str]], fallback: str) -> str:
    dates = sorted(str(row.get("time", "")) for row in history_rows if row.get("time"))
    if not dates:
        return fallback
    try:
        trading_day = datetime.strptime(dates[-1], "%Y%m%d").date()
    except ValueError:
        return fallback
    local_close = datetime.combine(
        trading_day,
        time(hour=15),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return local_close.astimezone(UTC).isoformat()


def plan_ifind_requests(
    *,
    ticker: str,
    report_year: int,
    as_of: date,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Plan the proxy requests required for one canonical research bundle."""

    ticker = _validate_ticker(ticker)
    latest_report = f"{report_year}1231"
    base_report = f"{report_year - 3}1231"
    history_start = as_of - timedelta(days=400)

    requests: dict[str, tuple[str, dict[str, Any]]] = {
        "info": (
            "/stock/info",
            {"ticker": ticker, "request_time": as_of.isoformat()},
        ),
        "history": (
            "/stock/history",
            {
                "ticker": ticker,
                "start_date": history_start.isoformat(),
                "end_date": as_of.isoformat(),
                "interval": "D",
                "adjust": "forward",
            },
        ),
        "income_latest": (
            "/financial/statements",
            {"ticker": ticker, "report_date": latest_report, "statement": "income_statement"},
        ),
        "income_base": (
            "/financial/statements",
            {"ticker": ticker, "report_date": base_report, "statement": "income_statement"},
        ),
        "cash_latest": (
            "/financial/statements",
            {"ticker": ticker, "report_date": latest_report, "statement": "cash_flow"},
        ),
        "capital_latest": (
            "/financial/index",
            {"ticker": ticker, "report_date": latest_report, "category": "capital_structure"},
        ),
    }
    for year in range(report_year - 3, report_year + 1):
        requests[f"profitability_{year}"] = (
            "/financial/index",
            {"ticker": ticker, "report_date": f"{year}1231", "category": "profitability"},
        )
    return requests


def _validate_ticker(ticker: str) -> str:
    if _TICKER_PATTERN.fullmatch(ticker.upper()) is None:
        raise ValueError("ticker must look like 600406.SH, 000001.SZ, or 430047.BJ")
    return ticker.upper()


def canonicalize_ifind_results(
    *,
    ticker: str,
    industry: str,
    report_year: int,
    results: dict[str, IfindProxyResult],
    retrieved_at: str,
    as_of: date | None = None,
) -> CanonicalBundle:
    """Normalize one ticker's decoded proxy responses to the canonical contract.

    Pure and offline: the caller supplies already-decoded ``IfindProxyResult``
    objects plus the retrieval timestamp, so saved provider fixtures can be
    replayed to byte-identical canonical data without network access.
    ``as_of`` anchors the trailing one-year market window; when omitted the
    latest returned trading session is used.
    """

    ticker = _validate_ticker(ticker)
    match = _TICKER_PATTERN.fullmatch(ticker)
    assert match is not None
    symbol = match.group("symbol")
    market_code = match.group("market")

    info = _first_row(results["info"], "stock info")
    history_rows = results["history"].rows
    if not history_rows:
        raise ValueError("iFinD returned no daily price history")
    history_rows = sorted(history_rows, key=lambda row: str(row.get("time", "")))
    quote_at = _quote_timestamp(history_rows, retrieved_at)
    # Required endpoints of the latest fiscal year: fail hard when empty.
    _first_row(results[f"profitability_{report_year}"], "profitability")
    _first_row(results["income_latest"], "latest income statement")
    _first_row(results["cash_latest"], "cash flow statement")
    _first_row(results["capital_latest"], "capital structure")

    # Every derived metric comes from the factor library over an observation
    # view of these rows (P1-B4; one definition per metric, decision D3).
    view = ifind_research_view(
        ticker=ticker,
        as_of=as_of or _latest_session(history_rows),
        report_year=report_year,
        rows={name: result.rows for name, result in results.items()},
    )
    factors = {
        name: DEFAULT_REGISTRY.get(name).compute(view, enforce_applicability=False)
        for name in _OBSERVATION_FACTORS.values()
    }

    def factor(field: str) -> float | None:
        return factors[_OBSERVATION_FACTORS[field]].value

    latest_bar = view.latest_raw_bar()
    latest_price = latest_bar.close if latest_bar is not None else None
    latest = view.latest_annual()
    assert latest is not None  # profitability_<report_year> is required above
    market_cap = factor("market_cap")
    returns = factors["return_1y"]
    one_year_sessions = int(returns.inputs_ref.get("sessions", 0))
    short_history = returns.value is not None and FLAG_SHORT_HISTORY in returns.flags

    # Valuation multiples are only meaningful over a positive denominator: a
    # loss-making year (EPS <= 0), negative book value or non-positive revenue
    # per share yields ``None`` plus a visible quality flag, never a negative
    # multiple that downstream thresholds would read as "cheap".
    not_meaningful: dict[str, tuple[str, str]] = {}
    for field, per_share_field, flag, template in _NOT_MEANINGFUL_MULTIPLES:
        per_share = latest.value(per_share_field)
        if flag in factors[_OBSERVATION_FACTORS[field]].flags and per_share is not None:
            not_meaningful[field] = (
                flag,
                template.format(year=report_year, value=f"{per_share:g}"),
            )

    stock_fields = ("symbol", "name", "market", "industry", "market_cap")
    stock_item = with_quality(
        {
            "symbol": symbol,
            "name": info.get("ths_stock_short_name_stock") or symbol,
            "market": market_code,
            "industry": industry,
            "market_cap": market_cap,
            "source": "ifind-proxy:stock-info+history",
            "updated_at": retrieved_at,
            "field_sources": {
                "name": _source("name", "/stock/info", retrieved_at),
                "market_cap": _source(
                    "market_cap",
                    "/stock/info + /stock/history",
                    quote_at,
                    quality="derived",
                    note="latest close multiplied by total shares",
                ),
                "industry": {
                    "field": "industry",
                    "provider": "manual",
                    "source": "command_input",
                    "quality": "direct",
                    "updated_at": retrieved_at,
                    "note": "iFinD stock-info response did not include a normalized industry",
                },
            },
        },
        stock_fields,
        derived_fields=("market_cap",),
    )

    market_fields = (
        "latest_price",
        "pe",
        "pb",
        "ps",
        "dividend_yield",
        "return_1y",
        "volatility_1y",
        "max_drawdown_1y",
        "market_cap",
    )
    market_item = with_quality(
        {
            "symbol": symbol,
            "latest_price": latest_price,
            "pe": factor("pe"),
            "pb": factor("pb"),
            "ps": factor("ps"),
            "dividend_yield": None,
            "return_1y": factor("return_1y"),
            "volatility_1y": factor("volatility_1y"),
            "max_drawdown_1y": factor("max_drawdown_1y"),
            "market_cap": market_cap,
            "source": "ifind-proxy:history+financial-index",
            "updated_at": quote_at,
            "field_sources": {
                "latest_price": _source("latest_price", "/stock/history", quote_at),
                "return_1y": _source(
                    "return_1y",
                    "/stock/history",
                    quote_at,
                    quality="derived",
                    note=f"trailing {ONE_YEAR_WINDOW_DAYS} days, {one_year_sessions} sessions",
                ),
                "volatility_1y": _source(
                    "volatility_1y", "/stock/history", quote_at, quality="derived"
                ),
                "max_drawdown_1y": _source(
                    "max_drawdown_1y", "/stock/history", quote_at, quality="derived"
                ),
                "market_cap": _source(
                    "market_cap",
                    "/stock/info + /stock/history",
                    quote_at,
                    quality="derived",
                ),
                "pe": _source(
                    "pe",
                    "/stock/history + /financial/index",
                    quote_at,
                    quality="derived",
                    note=f"latest close divided by FY{report_year} basic EPS",
                ),
                "pb": _source(
                    "pb",
                    "/stock/history + /financial/index",
                    quote_at,
                    quality="derived",
                    note=f"latest close divided by FY{report_year} net assets per share",
                ),
                "ps": _source(
                    "ps",
                    "/stock/history + /financial/index",
                    quote_at,
                    quality="derived",
                    note=f"latest close divided by FY{report_year} revenue per share",
                ),
                "dividend_yield": _source(
                    "dividend_yield",
                    "unavailable",
                    retrieved_at,
                    quality="missing",
                    note="the validated iFinD endpoint set did not expose cash dividends",
                ),
            },
        },
        market_fields,
        not_meaningful=not_meaningful,
        extra_flags=(
            [
                {
                    "field": field,
                    "flag": "short_price_history",
                    "message": (
                        f"{field} covers only {one_year_sessions} trading sessions "
                        f"(< {MIN_ONE_YEAR_SESSIONS}) within the trailing "
                        f"{ONE_YEAR_WINDOW_DAYS}-day window"
                    ),
                    "provider": "ifind",
                    "severity": "review",
                }
                for field in ("return_1y", "volatility_1y", "max_drawdown_1y")
            ]
            if short_history
            else []
        ),
        derived_fields=(
            "pe",
            "pb",
            "ps",
            "return_1y",
            "volatility_1y",
            "max_drawdown_1y",
            "market_cap",
        ),
    )

    financial_fields = (
        "revenue",
        "net_profit",
        "gross_margin",
        "net_margin",
        "roe",
        "roe_stability",
        "debt_ratio",
        "operating_cash_flow",
        "free_cash_flow",
        "revenue_growth_3y",
        "net_profit_growth_3y",
    )
    financial_item = with_quality(
        {
            "symbol": symbol,
            "revenue": latest.value("revenue_total"),
            "net_profit": latest.value("net_profit_parent"),
            "gross_margin": factor("gross_margin"),
            "net_margin": factor("net_margin"),
            "roe": factor("roe"),
            "roe_stability": factor("roe_stability"),
            "debt_ratio": factor("debt_ratio"),
            "operating_cash_flow": latest.value("operating_cash_flow"),
            "free_cash_flow": factor("free_cash_flow"),
            "revenue_growth_3y": factor("revenue_growth_3y"),
            "net_profit_growth_3y": factor("net_profit_growth_3y"),
            "source": "ifind-proxy:financial-statements+financial-index",
            "updated_at": retrieved_at,
            "field_sources": {
                field: _source(
                    field,
                    (
                        "/financial/statements"
                        if field
                        in {
                            "revenue",
                            "net_profit",
                            "operating_cash_flow",
                            "free_cash_flow",
                            "revenue_growth_3y",
                            "net_profit_growth_3y",
                        }
                        else "/financial/index"
                    ),
                    retrieved_at,
                    quality=(
                        "derived"
                        if field
                        in {
                            "roe_stability",
                            "free_cash_flow",
                            "revenue_growth_3y",
                            "net_profit_growth_3y",
                        }
                        else "direct"
                    ),
                    note=f"FY{report_year} with FY{report_year - 3} base where applicable",
                )
                for field in financial_fields
            },
        },
        financial_fields,
        derived_fields=(
            "roe_stability",
            "free_cash_flow",
            "revenue_growth_3y",
            "net_profit_growth_3y",
        ),
    )

    pool = {
        "metadata": {
            "source": "ifind-proxy",
            "updated_at": retrieved_at,
            "provider": "ifind",
            "payload_type": "stock_pool",
            "schema_version": CANONICAL_SCHEMA_VERSION,
        },
        "items": [stock_item],
    }
    market = {
        "metadata": {
            "source": "ifind-proxy",
            "updated_at": quote_at,
            "provider": "ifind",
            "payload_type": "market",
            "schema_version": CANONICAL_SCHEMA_VERSION,
        },
        "items": [market_item],
    }
    financial = {
        "metadata": {
            "source": "ifind-proxy",
            "updated_at": retrieved_at,
            "provider": "ifind",
            "payload_type": "financial",
            "report_year": report_year,
            "schema_version": CANONICAL_SCHEMA_VERSION,
        },
        "items": [financial_item],
    }
    raw = {
        "schema_version": "ifind-raw.v1",
        "ticker": ticker,
        "retrieved_at": retrieved_at,
        "requests": {
            name: {"path": result.path, "body": result.body} for name, result in results.items()
        },
    }
    validated = CanonicalResearchBundle.model_validate(
        {"pool": pool, "market": market, "financial": financial}
    )
    return CanonicalBundle(
        pool=validated.pool.model_dump(mode="json"),
        market=validated.market.model_dump(mode="json"),
        financial=validated.financial.model_dump(mode="json"),
        raw=raw,
    )


async def fetch_canonical_bundle(
    client: IfindProxyClient,
    *,
    ticker: str,
    industry: str,
    report_year: int,
    as_of: date,
) -> CanonicalBundle:
    """Fetch one stock through the proxy and normalize it to canonical data."""

    requests = plan_ifind_requests(ticker=ticker, report_year=report_year, as_of=as_of)
    names = list(requests)
    results_list = await asyncio.gather(*(client.post_rows(*requests[name]) for name in names))
    results = dict(zip(names, results_list, strict=True))
    return canonicalize_ifind_results(
        ticker=ticker,
        industry=industry,
        report_year=report_year,
        results=results,
        retrieved_at=datetime.now(UTC).isoformat(),
        as_of=as_of,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


_MISSING = object()
_NEEDS_REVIEW_LABEL = re.compile(r"(?<![A-Za-z0-9_])needs_review(?![A-Za-z0-9_])")


def _resolve_path(payload: dict[str, Any], path: str) -> Any:
    """Return the value at a dotted path, or ``_MISSING`` when it does not exist."""

    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _path_exists(payload: dict[str, Any], path: str) -> bool:
    return _resolve_path(payload, path) is not _MISSING


def _cites_missing_value(score_item: dict[str, Any], path: str, value: Any) -> bool:
    """True when a citation points at a null value or a field the kernel marked missing."""

    if value is None:
        return True
    if isinstance(value, dict | list):
        # Containers (e.g. field_sources.<payload>.<field>) document provenance,
        # including provenance of a missing field; they are not the value itself.
        return False
    missing = {str(field) for field in score_item.get("missing_fields", [])}
    return path.rsplit(".", 1)[-1] in missing


def validate_explanation_fields(
    explanation: ResearchExplanation,
    score_item: dict[str, Any],
) -> None:
    """Reject citations that do not resolve or that point at missing values.

    Evidence claims (classification, positive, negative, risk) must be backed
    by an actual value; citing a null or a ``missing_fields`` entry would let
    the agent build an argument on data that does not exist. Data-quality
    notes are the one place where citing a missing value is the point, so they
    only need the path to exist.
    """

    evidence_claims = (
        explanation.classification_rationale
        + explanation.positive_evidence
        + explanation.negative_evidence
        + explanation.risk_factors
    )
    unknown: set[str] = set()
    missing_backed: set[str] = set()
    for claim in evidence_claims:
        for field in claim.supporting_fields:
            value = _resolve_path(score_item, field)
            if value is _MISSING:
                unknown.add(field)
            elif _cites_missing_value(score_item, field, value):
                missing_backed.add(field)
    for claim in explanation.data_quality_notes:
        for field in claim.supporting_fields:
            if not _path_exists(score_item, field):
                unknown.add(field)
    if unknown:
        raise ValueError(f"Agent cited unknown canonical fields: {', '.join(sorted(unknown))}")
    if missing_backed:
        raise ValueError(
            "Agent cited missing or null fields as evidence: "
            + ", ".join(sorted(missing_backed))
        )


def validate_explanation_policy(
    explanation: ResearchExplanation,
    score_item: dict[str, Any],
) -> None:
    """Enforce the explanation contract: citations resolve and labels hold.

    The explanation agent may only cite canonical score fields that exist
    (and, outside data-quality notes, that carry a value), and when the
    deterministic kernel flagged ``needs_agent_review`` the workflow
    conclusion must carry the exact ``needs_review`` label token (not a
    substring such as ``no_needs_review``). Trading instructions are
    impossible by schema: ``ResearchExplanation`` has no order, position or
    return-promise fields.
    """

    validate_explanation_fields(explanation, score_item)
    if score_item.get("needs_agent_review") and not _NEEDS_REVIEW_LABEL.search(
        explanation.workflow_conclusion
    ):
        raise ValueError("Explanation did not preserve the required needs_review workflow label")


EXPLANATION_SYSTEM_PROMPT_TEMPLATE = """你是个人投研系统中的解释 Agent。只解释输入 JSON 中已经生成的事实与分数，不能补充外部事实，不能给出买入、卖出、仓位或收益承诺。
每条 classification_rationale、positive_evidence、negative_evidence、risk_factors、data_quality_notes 都必须给出 supporting_fields，并使用输入 JSON 中真实存在的点路径，例如 business_score、key_metrics.roe、factor_scores.growth_durability、missing_fields。
值为 null 或列在 missing_fields 中的字段不能作为分类、正面、负面或风险论据，只能在 data_quality_notes 中引用。
如果 needs_agent_review 为 true，workflow_conclusion 必须是包含“needs_review”的字符串，并优先解释 missing_fields、quality_flags、freshness 和 warnings。输出中文，只返回满足以下 JSON Schema 的单个 JSON 对象，不要省略字段：
{output_schema}"""


# One extra call when the model's JSON misses the schema (dropped fields,
# wrong types); the retry tells it exactly what failed.
EXPLANATION_SCHEMA_ATTEMPTS = 2


def _schema_retry_message(score_item: dict[str, Any], exc: ValidationError) -> str:
    problems = "\n".join(
        f"- {'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors()
    )
    return (
        json.dumps(score_item, ensure_ascii=False, sort_keys=True)
        + "\n\n上一次输出不符合 JSON Schema：\n"
        + problems
        + "\n请重新输出完整 JSON 对象，包含全部字段，workflow_conclusion 必须是字符串。"
    )


async def _explain_score(
    *,
    settings: Settings,
    run_id: int,
    score_item: dict[str, Any],
) -> ResearchExplanation:
    output_schema = json.dumps(
        ResearchExplanation.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
    )
    system_prompt = (
        EXPLANATION_SYSTEM_PROMPT_TEMPLATE.format(output_schema=output_schema)
        + "\n\n"
        + readability_rules()
    )
    async with session_scope() as session:
        client = OpenAICompatibleClient(
            tier_name="tier3",
            config=settings.llm_tier3,
            session=session,
        )
        user_message = json.dumps(score_item, ensure_ascii=False, sort_keys=True)
        for attempt in range(EXPLANATION_SCHEMA_ATTEMPTS):
            try:
                result = await client.complete(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    output_model=ResearchExplanation,
                    purpose="ifind_research_explanation",
                    pipeline_run_id=run_id,
                )
                break
            except ValidationError as exc:
                if attempt + 1 >= EXPLANATION_SCHEMA_ATTEMPTS:
                    raise
                user_message = _schema_retry_message(score_item, exc)
    explanation = result.output
    if not isinstance(explanation, ResearchExplanation):
        raise ValueError("Explanation agent did not return ResearchExplanation")
    validate_explanation_policy(explanation, score_item)
    return explanation


_REPORT_TITLES = {
    "ifind": "# iFinD 单股研究验证报告",
    "snapshot": "# 单股研究报告（免费源快照）",
}


def render_agent_report(
    *,
    score_item: dict[str, Any],
    explanation: ResearchExplanation,
    source_mode: str = "ifind",
) -> str:
    lines = [
        _REPORT_TITLES.get(source_mode, _REPORT_TITLES["ifind"]),
        "",
        "研究参考，非投资建议；所有 Agent 论据均须回溯到下列 canonical 字段。",
        "",
        f"- 股票：{score_item.get('name')}（{score_item.get('symbol')}.{score_item.get('market')}）",
        f"- 确定性业务分：{score_item.get('business_score')}",
        f"- 研究置信度：{score_item.get('research_confidence_score')}",
        f"- 工作流标签：{score_item.get('suggestion')}",
        f"- 需要复核：{score_item.get('needs_agent_review')}",
        "",
        "## Agent 摘要",
        "",
        explanation.summary,
    ]

    sections = (
        ("分类依据", explanation.classification_rationale),
        ("正面证据", explanation.positive_evidence),
        ("负面证据", explanation.negative_evidence),
        ("风险因素", explanation.risk_factors),
        ("数据质量", explanation.data_quality_notes),
    )
    for title, claims in sections:
        lines.extend(["", f"## {title}", ""])
        if not claims:
            lines.append("- 无")
            continue
        for claim in claims:
            fields = ", ".join(f"`{field}`" for field in claim.supporting_fields)
            lines.append(f"- {claim.text}（依据：{fields}）")

    lines.extend(["", "## 待核问题", ""])
    lines.extend(f"- {question}" for question in explanation.open_questions)
    lines.extend(["", "## 研究工作流结论", "", explanation.workflow_conclusion, ""])
    return "\n".join(lines)


def _artifact_dir_for_attempt(
    *, output_root: Path, run_id: int, attempt_count: int
) -> Path:
    run_dir = output_root / str(run_id)
    return run_dir if attempt_count <= 1 else run_dir / f"attempt-{attempt_count}"


def score_source_mode(score: dict[str, Any] | None) -> str | None:
    """Source mode of a stored score; pre-P1-B5 scores came from iFinD."""

    if score is None:
        return None
    value = score.get("source_mode")
    return str(value) if isinstance(value, str) and value else "ifind"


async def _previous_source_mode(session: Any, *, symbol: str, market: str) -> str | None:
    previous = await ResearchSnapshotRepository(session).latest(symbol=symbol, market=market)
    return None if previous is None else score_source_mode(previous.score_json)


async def _finish_snapshot_missing(
    *,
    run_id: int,
    ticker: str,
    as_of: date,
    report_year: int | None,
    error: Exception,
) -> str:
    """Snapshot store cannot answer: queue an A13 ``missing`` request, end ``partial``.

    No iFinD call is made here; the verification job fetches within budget.
    """

    from investment_agent.research.snapshot_source import SnapshotDataMissingError
    from investment_agent.verification.queue import enqueue_verification

    listing_id = error.listing_id if isinstance(error, SnapshotDataMissingError) else None
    detail = str(getattr(error, "reason", error))
    queued = False
    if listing_id is not None:
        await enqueue_verification(
            listing_id=listing_id,
            ticker=ticker,
            trigger="missing",
            fields=None,
            report_period=date(report_year, 12, 31) if report_year is not None else None,
            detail={"source": "research_snapshot", "reason": detail, "as_of": as_of.isoformat()},
            run_id=run_id,
        )
        queued = True
    reason = (
        f"snapshot_data_missing: {detail}; "
        + (
            "iFinD verification request (trigger=missing) queued"
            if queued
            else "listing unknown, no verification request queued"
        )
    )
    metadata: dict[str, object] = {
        "source_mode": "snapshot",
        "partial_reason": "snapshot_data_missing",
        "missing_detail": detail,
        "verification_requested": queued,
    }
    async with session_scope() as session:
        await PipelineRunRepository(session).mark_partial(run_id, reason, metadata)
    return reason


async def _request_missing_key_fields(
    *,
    run_id: int,
    ticker: str,
    report_year: int | None,
    score_item: dict[str, Any],
) -> list[str]:
    """Snapshot mode: queue an A13 ``missing`` request for absent key fields.

    Best effort (never fails the run); the open request dedups repeats, and
    once iFinD verified a value the view resolves it, so it is asked once.
    """

    from investment_agent.research.snapshot_source import missing_key_fields
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.verification.queue import enqueue_verification

    fields = missing_key_fields(score_item)
    if not fields:
        return []
    try:
        async with session_scope() as session:
            listing = await ListingRepository(session).get_a_share_listing(ticker)
        if listing is None:
            return []
        await enqueue_verification(
            listing_id=listing.id,
            ticker=ticker,
            trigger="missing",
            fields=fields,
            report_period=date(report_year, 12, 31) if report_year is not None else None,
            detail={"source": "research_snapshot", "missing_fields": score_item.get("missing_fields")},
            run_id=run_id,
        )
    except Exception:  # noqa: BLE001 - the score stands without the request
        import structlog

        structlog.get_logger(__name__).exception(
            "research.snapshot.verification_request_failed", run_id=run_id, ticker=ticker
        )
        return []
    return fields


async def run_ifind_research_chain(
    *,
    settings: Settings,
    proxy_url: str,
    proxy_api_key: str,
    ticker: str,
    industry: str | None,
    report_year: int | None,
    as_of: date,
    output_root: Path,
    kernel: DeterministicResearchKernel | None = None,
    triggered_by: str = "cli",
    idempotency_key: str | None = None,
    claimed_run_id: int | None = None,
    source_mode: str = "ifind",
    industry_peer_context: dict[str, object] | None = None,
) -> ResearchChainResult:
    """Run and persist the complete data -> score -> explanation vertical slice.

    ``source_mode`` (P1-B5):

    * ``ifind`` - manual deep research through ifind-proxy (every call counted
      in the A13 budget ledger); ``industry`` and ``report_year`` required;
    * ``snapshot`` - the canonical bundle comes from the free-source snapshot
      store (:mod:`investment_agent.research.snapshot_source`), no iFinD call.
      ``industry`` is optional (the data industry group is used without it);
      the latest annual report public on ``as_of`` is used. When the store
      has no data for the listing, a ``missing`` iFinD verification request is
      queued (A13) and the run ends ``partial`` without a score.

    The run metadata and the snapshot's score record ``source_mode``; the
    first run after a switch carries ``source_changed`` (see change tracking).

    ``industry_peer_context`` (WP9) is the trusted peer-context binding frozen
    at enqueue time; it is stored verbatim in the snapshot's nullable
    ``industry_peer_context`` column and returned by the run detail. It is
    never set by the HTTP submit path - the trusted application freezes it.
    """

    if source_mode not in {"ifind", "snapshot"}:
        raise ValueError(f"unknown research source mode {source_mode!r}")
    if source_mode == "ifind" and (not industry or report_year is None):
        raise ValueError("iFinD research needs industry and report_year")

    async with session_scope() as session:
        run_repository = PipelineRunRepository(session)
        snapshot_repository = ResearchSnapshotRepository(session)
        if claimed_run_id is not None:
            run = await run_repository.get(claimed_run_id)
            if run is None:
                raise ValueError(f"PipelineRun {claimed_run_id} not found")
            if run.kind != "ifind_research" or run.status != "running":
                raise ValueError(
                    f"PipelineRun {claimed_run_id} is not a claimed research run"
                )
        elif idempotency_key:
            run, created = await run_repository.enqueue(
                kind="ifind_research",
                idempotency_key=idempotency_key,
                triggered_by=triggered_by,
            )
            if not created:
                snapshot = await snapshot_repository.by_run(run.id)
                if run.status == "success" and snapshot is not None:
                    explanation = ResearchExplanation.model_validate(
                        snapshot.explanation_json
                    )
                    agent_report = snapshot.artifact_manifest.get("agent_report")
                    if not isinstance(agent_report, str):
                        raise ValueError(
                            f"research snapshot {snapshot.id} has no agent_report artifact"
                        )
                    return ResearchChainResult(
                        run_id=run.id,
                        artifact_dir=Path(agent_report).parent,
                        score=snapshot.score_json,
                        explanation=explanation,
                    )
                raise ResearchRunConflictError(run_id=run.id, status=run.status)
        else:
            run = await run_repository.create(
                kind="ifind_research",
                triggered_by=triggered_by,
                status="queued",
                stage="queued",
            )
        if claimed_run_id is None:
            await run_repository.mark_running(run.id, stage="fetching")
        else:
            await run_repository.set_stage(run.id, "fetching")
        run_id = run.id
        attempt_count = run.attempt_count

    artifact_dir = _artifact_dir_for_attempt(
        output_root=output_root,
        run_id=run_id,
        attempt_count=attempt_count,
    )
    try:
        artifact_dir.mkdir(parents=True, exist_ok=False)
        if source_mode == "snapshot":
            from investment_agent.research.snapshot_source import (
                SnapshotDataMissingError,
                load_snapshot_bundle,
            )

            try:
                async with session_scope() as session:
                    loaded = await load_snapshot_bundle(
                        session,
                        ticker=ticker,
                        as_of=as_of,
                        industry=industry,
                        requested_report_year=report_year,
                    )
            except SnapshotDataMissingError as exc:
                reason = await _finish_snapshot_missing(
                    run_id=run_id,
                    ticker=ticker,
                    as_of=as_of,
                    report_year=report_year,
                    error=exc,
                )
                raise ResearchPartialError(run_id=run_id, reason=reason) from exc
            bundle = loaded.bundle
            report_year = loaded.report_year
            raw_name = "raw_snapshot.json"
        else:
            from investment_agent.verification.budget import ledger_hook

            assert industry is not None and report_year is not None
            async with IfindProxyClient(
                base_url=proxy_url,
                api_key=proxy_api_key,
                on_call=ledger_hook(source="research", run_id=run_id),
            ) as client:
                bundle = await fetch_canonical_bundle(
                    client,
                    ticker=ticker,
                    industry=industry,
                    report_year=report_year,
                    as_of=as_of,
                )
            raw_name = "raw_ifind.json"

        async with session_scope() as session:
            await PipelineRunRepository(session).set_stage(run_id, "scoring")

        pool_path = artifact_dir / "pool.json"
        market_path = artifact_dir / "market.json"
        financial_path = artifact_dir / "financial.json"
        _write_json(pool_path, bundle.pool)
        _write_json(market_path, bundle.market)
        _write_json(financial_path, bundle.financial)
        _write_json(artifact_dir / raw_name, bundle.raw)

        kernel_artifacts = (kernel or InProcessResearchKernel()).score_and_render(
            pool=pool_path,
            market=market_path,
            financial=financial_path,
            artifact_dir=artifact_dir,
            as_of=datetime.combine(
                as_of,
                time.max,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).astimezone(UTC),
        )
        scores = json.loads(kernel_artifacts.scores_json.read_text(encoding="utf-8"))
        score_item = scores["items"][0]
        artifact_manifest: dict[str, object] = {
            "pool": str(pool_path.resolve()),
            "market": str(market_path.resolve()),
            "financial": str(financial_path.resolve()),
            "raw_provider": str((artifact_dir / raw_name).resolve()),
            "scores": str(kernel_artifacts.scores_json.resolve()),
            "scores_csv": str(kernel_artifacts.scores_csv.resolve()),
            "report_context": str(kernel_artifacts.report_context_json.resolve()),
            "deterministic_report": str(kernel_artifacts.report_markdown.resolve()),
        }
        score_item["source_mode"] = source_mode
        verification_fields: list[str] = []
        if source_mode == "snapshot":
            verification_fields = await _request_missing_key_fields(
                run_id=run_id,
                ticker=ticker,
                report_year=report_year,
                score_item=score_item,
            )
        async with session_scope() as session:
            previous_mode = await _previous_source_mode(
                session, symbol=str(score_item["symbol"]), market=str(score_item["market"])
            )
        source_changed = previous_mode is not None and previous_mode != source_mode
        metadata: dict[str, object] = {
            "symbol": score_item["symbol"],
            "market": score_item["market"],
            "provider": "ifind-proxy" if source_mode == "ifind" else "snapshot_view",
            "source_mode": source_mode,
            "previous_source_mode": previous_mode,
            "source_changed": source_changed,
            "verification_requested_fields": verification_fields,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "scoring_version": score_item["scoring_version"],
            "score_schema_version": score_item["schema_version"],
            "business_score": score_item["business_score"],
            "research_confidence_score": score_item["research_confidence_score"],
            "suggestion": score_item["suggestion"],
            "needs_agent_review": score_item["needs_agent_review"],
            "artifact_dir": str(artifact_dir.resolve()),
        }
        async with session_scope() as session:
            await PipelineRunRepository(session).set_stage(run_id, "explaining")
        try:
            explanation = await _explain_score(
                settings=settings,
                run_id=run_id,
                score_item=score_item,
            )
        except Exception as exc:
            from investment_agent.research.retry_policy import (
                ResearchRetryableError,
                is_transient_error,
            )

            reason = f"explanation failed: {type(exc).__name__}: {exc}"
            if claimed_run_id is not None and is_transient_error(exc):
                # LLM timeout / rate limit / connection failure under a worker
                # claim: let the worker retry while budget remains. On the last
                # attempt fall through to `partial`, keeping the deterministic
                # score instead of losing the run.
                async with session_scope() as session:
                    current = await PipelineRunRepository(session).get(run_id)
                if current is not None and current.attempt_count < current.max_attempts:
                    raise ResearchRetryableError(run_id=run_id, reason=reason) from exc
            metadata["partial_reason"] = "explanation_failed"
            async with session_scope() as session:
                await PipelineRunRepository(session).set_stage(run_id, "persisting")
                await ResearchSnapshotRepository(session).create(
                    pipeline_run_id=run_id,
                    symbol=str(score_item["symbol"]),
                    market=str(score_item["market"]),
                    company_name=str(score_item["name"]),
                    industry=str(score_item["industry"]),
                    as_of=as_of,
                    report_year=report_year,
                    canonical_schema_version=CANONICAL_SCHEMA_VERSION,
                    scoring_version=str(score_item["scoring_version"]),
                    score_schema_version=str(score_item["schema_version"]),
                    score_json=score_item,
                    explanation_json=None,
                    artifact_manifest=artifact_manifest,
                    industry_peer_context=industry_peer_context,
                )
                await PipelineRunRepository(session).mark_partial(
                    run_id,
                    reason,
                    metadata,
                )
            raise ResearchPartialError(run_id=run_id, reason=reason) from exc
        explanation_path = artifact_dir / "agent_explanation.json"
        report_path = artifact_dir / "agent_report.md"
        _write_json(explanation_path, explanation.model_dump())
        report_path.write_text(
            render_agent_report(
                score_item=score_item, explanation=explanation, source_mode=source_mode
            ),
            encoding="utf-8",
        )

        artifact_manifest["agent_explanation"] = str(explanation_path.resolve())
        artifact_manifest["agent_report"] = str(report_path.resolve())
        metadata["agent_report"] = str(report_path.resolve())
        async with session_scope() as session:
            await PipelineRunRepository(session).set_stage(run_id, "persisting")
            await ResearchSnapshotRepository(session).create(
                pipeline_run_id=run_id,
                symbol=str(score_item["symbol"]),
                market=str(score_item["market"]),
                company_name=str(score_item["name"]),
                industry=str(score_item["industry"]),
                as_of=as_of,
                report_year=report_year,
                canonical_schema_version=CANONICAL_SCHEMA_VERSION,
                scoring_version=str(score_item["scoring_version"]),
                score_schema_version=str(score_item["schema_version"]),
                score_json=score_item,
                explanation_json=explanation.model_dump(mode="json"),
                artifact_manifest=artifact_manifest,
                industry_peer_context=industry_peer_context,
            )
            await PipelineRunRepository(session).mark_success(run_id, metadata)
        return ResearchChainResult(
            run_id=run_id,
            artifact_dir=artifact_dir,
            score=score_item,
            explanation=explanation,
        )
    except ResearchPartialError:
        raise
    except Exception as exc:
        # Under a worker claim the worker owns the single retry-or-terminal
        # decision (atomically, fenced by its lease); persisting `failed` here
        # first would let a later retry flip a terminal status back to queued.
        if claimed_run_id is None:
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_failed(run_id, str(exc))
        raise
