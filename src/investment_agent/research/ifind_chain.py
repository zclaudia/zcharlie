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
import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from investment_agent.config import Settings
from investment_agent.llm.openai_compatible import OpenAICompatibleClient
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


class IfindProxyClient:
    """Small async client that unwraps the proxy's nested ``data_preview`` CSV."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 60.0):
        if not api_key:
            raise ValueError("IFIND_PROXY_API_KEY is required")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout_seconds,
        )

    async def __aenter__(self) -> IfindProxyClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def post_rows(self, path: str, payload: dict[str, Any]) -> IfindProxyResult:
        response = await self._client.post(path, json=payload)
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


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "None", "null", "nan"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _fraction(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number / 100.0


def _first_row(result: IfindProxyResult, name: str) -> dict[str, str]:
    if not result.rows:
        raise ValueError(f"iFinD returned no rows for {name}")
    return result.rows[0]


def _annualized_growth(latest: float | None, base: float | None, years: int) -> float | None:
    if latest is None or base is None or latest < 0 or base <= 0 or years <= 0:
        return None
    return round(float((latest / base) ** (1 / years) - 1), 6)


def _return_stats(rows: list[dict[str, str]]) -> tuple[float | None, float | None, float | None]:
    series = [
        (str(row.get("time", "")), close)
        for row in rows
        if (close := _number(row.get("close"))) is not None and close > 0
    ]
    series.sort(key=lambda item: item[0])
    prices = [value for _, value in series]
    if len(prices) < 2:
        return None, None, None

    total_return = prices[-1] / prices[0] - 1
    daily_returns = [
        current / previous - 1 for previous, current in zip(prices, prices[1:], strict=False)
    ]
    volatility = statistics.pstdev(daily_returns) * math.sqrt(252)

    peak = prices[0]
    max_drawdown = 0.0
    for price in prices:
        peak = max(peak, price)
        max_drawdown = max(max_drawdown, (peak - price) / peak)
    return round(total_return, 6), round(volatility, 6), round(max_drawdown, 6)


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


def _with_quality(
    item: dict[str, Any],
    required_fields: tuple[str, ...],
    *,
    derived_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    missing = [field for field in required_fields if item.get(field) is None]
    flags = [
        {
            "field": field,
            "flag": "missing_field",
            "message": f"iFinD normalization could not produce {field}",
            "provider": "ifind",
            "severity": "review",
        }
        for field in missing
    ]
    flags.extend(
        {
            "field": field,
            "flag": "derived_field",
            "message": f"{field} was derived from iFinD source fields",
            "provider": "ifind",
            "severity": "info",
        }
        for field in derived_fields
        if item.get(field) is not None
    )
    item["missing_fields"] = missing
    item["quality_flags"] = flags
    item["provider_conflicts"] = {}
    item["warnings"] = [flag["message"] for flag in flags]
    item["needs_agent_review"] = bool(missing)
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


async def fetch_canonical_bundle(
    client: IfindProxyClient,
    *,
    ticker: str,
    industry: str,
    report_year: int,
    as_of: date,
) -> CanonicalBundle:
    """Fetch one stock and normalize it to the canonical provider contract."""

    match = _TICKER_PATTERN.fullmatch(ticker.upper())
    if match is None:
        raise ValueError("ticker must look like 600406.SH, 000001.SZ, or 430047.BJ")
    ticker = ticker.upper()
    symbol = match.group("symbol")
    market_code = match.group("market")
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

    names = list(requests)
    results_list = await asyncio.gather(*(client.post_rows(*requests[name]) for name in names))
    results = dict(zip(names, results_list, strict=True))
    retrieved_at = datetime.now(UTC).isoformat()

    info = _first_row(results["info"], "stock info")
    history_rows = results["history"].rows
    if not history_rows:
        raise ValueError("iFinD returned no daily price history")
    history_rows = sorted(history_rows, key=lambda row: str(row.get("time", "")))
    quote_at = _quote_timestamp(history_rows, retrieved_at)
    latest_price = _number(history_rows[-1].get("close"))
    total_shares = _number(info.get("ths_total_shares_stock"))
    market_cap = (
        latest_price * total_shares
        if latest_price is not None and total_shares is not None
        else None
    )
    return_1y, volatility_1y, max_drawdown_1y = _return_stats(history_rows)

    profitability = _first_row(results[f"profitability_{report_year}"], "profitability")
    eps = _number(profitability.get("ths_eps_basic_stock"))
    book_value_per_share = _number(profitability.get("ths_nav_ps_stock"))
    revenue_per_share = _number(profitability.get("ths_revenue_ps_stock"))
    pe = latest_price / eps if latest_price is not None and eps is not None and eps != 0 else None
    pb = (
        latest_price / book_value_per_share
        if latest_price is not None
        and book_value_per_share is not None
        and book_value_per_share != 0
        else None
    )
    ps = (
        latest_price / revenue_per_share
        if latest_price is not None and revenue_per_share is not None and revenue_per_share != 0
        else None
    )

    income_latest = _first_row(results["income_latest"], "latest income statement")
    income_base = _first_row(results["income_base"], "base income statement")
    cash_latest = _first_row(results["cash_latest"], "cash flow statement")
    capital_latest = _first_row(results["capital_latest"], "capital structure")
    revenue = _number(income_latest.get("ths_operating_total_revenue_stock"))
    base_revenue = _number(income_base.get("ths_operating_total_revenue_stock"))
    net_profit = _number(income_latest.get("ths_np_atoopc_stock"))
    base_net_profit = _number(income_base.get("ths_np_atoopc_stock"))
    operating_cash_flow = _number(cash_latest.get("ths_ncf_from_oa_stock"))
    capital_expenditure = _number(cash_latest.get("ths_cash_paid_for_assets_stock"))
    free_cash_flow = (
        operating_cash_flow - capital_expenditure
        if operating_cash_flow is not None and capital_expenditure is not None
        else None
    )
    roe_values = [
        value
        for year in range(report_year - 3, report_year + 1)
        if (
            value := _fraction(
                _first_row(results[f"profitability_{year}"], f"profitability {year}").get(
                    "ths_roe_stock"
                )
            )
        )
        is not None
    ]
    roe_stability = None
    if roe_values:
        roe_mean = statistics.mean(roe_values)
        if roe_mean:
            roe_stability = round(
                max(0.0, min(1.0, 1 - statistics.pstdev(roe_values) / roe_mean)),
                6,
            )

    stock_fields = ("symbol", "name", "market", "industry", "market_cap")
    stock_item = _with_quality(
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
    market_item = _with_quality(
        {
            "symbol": symbol,
            "latest_price": latest_price,
            "pe": None if pe is None else round(pe, 4),
            "pb": None if pb is None else round(pb, 4),
            "ps": None if ps is None else round(ps, 4),
            "dividend_yield": None,
            "return_1y": return_1y,
            "volatility_1y": volatility_1y,
            "max_drawdown_1y": max_drawdown_1y,
            "market_cap": market_cap,
            "source": "ifind-proxy:history+financial-index",
            "updated_at": quote_at,
            "field_sources": {
                "latest_price": _source("latest_price", "/stock/history", quote_at),
                "return_1y": _source("return_1y", "/stock/history", quote_at, quality="derived"),
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
    financial_item = _with_quality(
        {
            "symbol": symbol,
            "revenue": revenue,
            "net_profit": net_profit,
            "gross_margin": _fraction(profitability.get("ths_gross_selling_rate_stock")),
            "net_margin": _fraction(profitability.get("ths_net_sales_rate_stock")),
            "roe": _fraction(profitability.get("ths_roe_stock")),
            "roe_stability": roe_stability,
            "debt_ratio": _fraction(capital_latest.get("ths_asset_liab_ratio_stock")),
            "operating_cash_flow": operating_cash_flow,
            "free_cash_flow": free_cash_flow,
            "revenue_growth_3y": _annualized_growth(revenue, base_revenue, 3),
            "net_profit_growth_3y": _annualized_growth(net_profit, base_net_profit, 3),
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


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _path_exists(payload: dict[str, Any], path: str) -> bool:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False
    return True


def validate_explanation_fields(
    explanation: ResearchExplanation,
    score_item: dict[str, Any],
) -> None:
    claims = (
        explanation.classification_rationale
        + explanation.positive_evidence
        + explanation.negative_evidence
        + explanation.risk_factors
        + explanation.data_quality_notes
    )
    invalid = sorted(
        {
            field
            for claim in claims
            for field in claim.supporting_fields
            if not _path_exists(score_item, field)
        }
    )
    if invalid:
        raise ValueError(f"Agent cited unknown canonical fields: {', '.join(invalid)}")


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
    system_prompt = f"""你是个人投研系统中的解释 Agent。只解释输入 JSON 中已经生成的事实与分数，不能补充外部事实，不能给出买入、卖出、仓位或收益承诺。
每条 classification_rationale、positive_evidence、negative_evidence、risk_factors、data_quality_notes 都必须给出 supporting_fields，并使用输入 JSON 中真实存在的点路径，例如 business_score、key_metrics.roe、factor_scores.growth_durability、missing_fields。
如果 needs_agent_review 为 true，workflow_conclusion 必须是包含“needs_review”的字符串，并优先解释 missing_fields、quality_flags、freshness 和 warnings。输出中文，只返回满足以下 JSON Schema 的单个 JSON 对象，不要省略字段：
{output_schema}"""
    async with session_scope() as session:
        client = OpenAICompatibleClient(
            tier_name="tier3",
            config=settings.llm_tier3,
            session=session,
        )
        result = await client.complete(
            system_prompt=system_prompt,
            user_message=json.dumps(score_item, ensure_ascii=False, sort_keys=True),
            output_model=ResearchExplanation,
            purpose="ifind_research_explanation",
            pipeline_run_id=run_id,
        )
    explanation = result.output
    if not isinstance(explanation, ResearchExplanation):
        raise ValueError("Explanation agent did not return ResearchExplanation")
    validate_explanation_fields(explanation, score_item)
    if (
        score_item.get("needs_agent_review")
        and "needs_review" not in explanation.workflow_conclusion
    ):
        raise ValueError("Explanation did not preserve the required needs_review workflow label")
    return explanation


def render_agent_report(
    *,
    score_item: dict[str, Any],
    explanation: ResearchExplanation,
) -> str:
    lines = [
        "# iFinD 单股研究验证报告",
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


async def run_ifind_research_chain(
    *,
    settings: Settings,
    proxy_url: str,
    proxy_api_key: str,
    ticker: str,
    industry: str,
    report_year: int,
    as_of: date,
    output_root: Path,
    kernel: DeterministicResearchKernel | None = None,
    triggered_by: str = "cli",
    idempotency_key: str | None = None,
    claimed_run_id: int | None = None,
) -> ResearchChainResult:
    """Run and persist the complete iFinD -> score -> explanation vertical slice."""

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
        async with IfindProxyClient(base_url=proxy_url, api_key=proxy_api_key) as client:
            bundle = await fetch_canonical_bundle(
                client,
                ticker=ticker,
                industry=industry,
                report_year=report_year,
                as_of=as_of,
            )

        async with session_scope() as session:
            await PipelineRunRepository(session).set_stage(run_id, "scoring")

        pool_path = artifact_dir / "pool.json"
        market_path = artifact_dir / "market.json"
        financial_path = artifact_dir / "financial.json"
        _write_json(pool_path, bundle.pool)
        _write_json(market_path, bundle.market)
        _write_json(financial_path, bundle.financial)
        _write_json(artifact_dir / "raw_ifind.json", bundle.raw)

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
        async with session_scope() as session:
            await PipelineRunRepository(session).set_stage(run_id, "explaining")
        explanation = await _explain_score(
            settings=settings,
            run_id=run_id,
            score_item=score_item,
        )
        explanation_path = artifact_dir / "agent_explanation.json"
        report_path = artifact_dir / "agent_report.md"
        _write_json(explanation_path, explanation.model_dump())
        report_path.write_text(
            render_agent_report(score_item=score_item, explanation=explanation),
            encoding="utf-8",
        )

        artifact_manifest: dict[str, object] = {
            "pool": str(pool_path.resolve()),
            "market": str(market_path.resolve()),
            "financial": str(financial_path.resolve()),
            "raw_provider": str((artifact_dir / "raw_ifind.json").resolve()),
            "scores": str(kernel_artifacts.scores_json.resolve()),
            "scores_csv": str(kernel_artifacts.scores_csv.resolve()),
            "report_context": str(kernel_artifacts.report_context_json.resolve()),
            "deterministic_report": str(kernel_artifacts.report_markdown.resolve()),
            "agent_explanation": str(explanation_path.resolve()),
            "agent_report": str(report_path.resolve()),
        }

        metadata: dict[str, object] = {
            "symbol": score_item["symbol"],
            "market": score_item["market"],
            "provider": "ifind-proxy",
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "scoring_version": score_item["scoring_version"],
            "score_schema_version": score_item["schema_version"],
            "business_score": score_item["business_score"],
            "research_confidence_score": score_item["research_confidence_score"],
            "suggestion": score_item["suggestion"],
            "needs_agent_review": score_item["needs_agent_review"],
            "artifact_dir": str(artifact_dir.resolve()),
            "agent_report": str(report_path.resolve()),
        }
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
            )
            await PipelineRunRepository(session).mark_success(run_id, metadata)
        return ResearchChainResult(
            run_id=run_id,
            artifact_dir=artifact_dir,
            score=score_item,
            explanation=explanation,
        )
    except Exception as exc:
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_failed(run_id, str(exc))
        raise
