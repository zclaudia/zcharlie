from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_agent.research_kernel import __version__
from investment_agent.research_kernel.company_profile import classify_company_profile
from investment_agent.research_kernel.models import (
    MODEL_LABELS,
    STRATEGY_LABELS,
    SUGGESTION_LABELS,
    normalize_number,
    utc_now_iso,
)
from investment_agent.research_kernel.scoring_models import (
    SECTOR_MODEL_KEYS,
    factor_scores_for_model,
    model_fit_score,
    ranked_strategies,
    select_scoring_model,
)

SCORING_VERSION = "v1.2"
SCHEMA_VERSION = "v2.0"
DEFAULT_MAX_MARKET_AGE_DAYS = 1
DEFAULT_MAX_FINANCIAL_AGE_DAYS = 180
DEFAULT_MAX_STOCK_AGE_DAYS = 30

CYCLE_INDUSTRY_KEYWORDS = ("有色", "煤炭", "石油", "化工", "钢铁", "航运", "能源", "矿")
VENTURE_INDUSTRY_KEYWORDS = ("半导体", "AI", "机器人", "创新药", "计算机", "电子")
GROWTH_INDUSTRY_KEYWORDS = ("电力设备", "新能源", "自动化", "汽车零部件", "医药", "高端制造")
DIVIDEND_INDUSTRY_KEYWORDS = ("公用事业", "银行", "运营商", "电力", "高速")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def metric(row: dict[str, Any], key: str) -> float | None:
    try:
        return normalize_number(row.get(key))
    except ValueError:
        return None


def score_ratio(value: float | None, low: float, high: float, *, inverse: bool = False) -> float:
    if value is None:
        return 50.0
    if high == low:
        return 50.0
    normalized = (value - low) / (high - low)
    if inverse:
        normalized = 1 - normalized
    return clamp(normalized * 100)


def industry_has(industry: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in industry.lower() for keyword in keywords)


def classify_stock(
    stock: dict[str, Any], market: dict[str, Any], financial: dict[str, Any]
) -> dict[str, Any]:
    industry = str(stock.get("industry", ""))
    market_cap = metric(stock, "market_cap")
    if market_cap is None:
        market_cap = metric(market, "market_cap")
    dividend_yield = metric(market, "dividend_yield")
    roe = metric(financial, "roe")
    revenue_growth = metric(financial, "revenue_growth_3y")
    free_cash_flow = metric(financial, "free_cash_flow")

    fit_scores = {
        "dividend_blue_chip": 0.0,
        "quality_growth": 0.0,
        "steady_quality_growth": 0.0,
        "cycle_reversal": 0.0,
        "high_growth_venture": 0.0,
    }

    if industry_has(industry, DIVIDEND_INDUSTRY_KEYWORDS):
        fit_scores["dividend_blue_chip"] += 35
    if market_cap is not None and market_cap >= 300_000_000_000:
        fit_scores["dividend_blue_chip"] += 20
    if dividend_yield is not None and dividend_yield >= 0.025:
        fit_scores["dividend_blue_chip"] += 25
    if free_cash_flow is not None and free_cash_flow > 0:
        fit_scores["dividend_blue_chip"] += 20

    if industry_has(industry, GROWTH_INDUSTRY_KEYWORDS):
        fit_scores["quality_growth"] += 35
    if revenue_growth is not None and revenue_growth >= 0.18:
        fit_scores["quality_growth"] += 35
    if roe is not None and roe >= 0.15:
        fit_scores["quality_growth"] += 20
    if market_cap is not None and market_cap >= 100_000_000_000:
        fit_scores["quality_growth"] += 10

    debt_ratio = metric(financial, "debt_ratio")
    if industry_has(industry, GROWTH_INDUSTRY_KEYWORDS):
        fit_scores["steady_quality_growth"] += 30
    if roe is not None and roe >= 0.12:
        fit_scores["steady_quality_growth"] += 20
    if revenue_growth is not None and 0.06 <= revenue_growth < 0.18:
        fit_scores["steady_quality_growth"] += 20
    if free_cash_flow is not None and free_cash_flow > 0:
        fit_scores["steady_quality_growth"] += 15
    if dividend_yield is not None and dividend_yield >= 0.015:
        fit_scores["steady_quality_growth"] += 10
    if debt_ratio is not None and debt_ratio <= 0.55:
        fit_scores["steady_quality_growth"] += 5

    if industry_has(industry, CYCLE_INDUSTRY_KEYWORDS):
        fit_scores["cycle_reversal"] += 65
    drawdown = metric(market, "max_drawdown_1y")
    if drawdown is not None and drawdown >= 0.30:
        fit_scores["cycle_reversal"] += 20
    if revenue_growth is not None and revenue_growth >= 0.10:
        fit_scores["cycle_reversal"] += 15

    if industry_has(industry, VENTURE_INDUSTRY_KEYWORDS):
        fit_scores["high_growth_venture"] += 55
    if revenue_growth is not None and revenue_growth >= 0.30:
        fit_scores["high_growth_venture"] += 25
    if market_cap is not None and market_cap < 100_000_000_000:
        fit_scores["high_growth_venture"] += 20

    ranked = sorted(fit_scores.items(), key=lambda item: item[1], reverse=True)
    strategy_type, top_score = ranked[0]
    second_score = ranked[1][1]
    confidence = 0.5 + min(0.49, max(0.0, top_score - second_score) / 100)
    needs_review = confidence < 0.6 or strategy_type == "high_growth_venture"

    return {
        "strategy_type": strategy_type,
        "strategy_label": STRATEGY_LABELS[strategy_type],
        "classification_confidence": round(confidence, 2),
        "classification_fit_scores": {key: round(value, 2) for key, value in fit_scores.items()},
        "needs_agent_review": needs_review,
    }


def valuation_level(pe: float | None, pb: float | None) -> str:
    if pe is None and pb is None:
        return "unknown"
    pe_value = pe if pe is not None else 100.0
    pb_value = pb if pb is not None else 10.0
    if pe_value <= 15 and pb_value <= 2.0:
        return "cheap"
    if pe_value <= 30 and pb_value <= 5.0:
        return "fair"
    return "expensive"


def risk_level(volatility: float | None, drawdown: float | None, debt_ratio: float | None) -> str:
    risk_points = 0
    if volatility is not None and volatility >= 0.40:
        risk_points += 1
    if drawdown is not None and drawdown >= 0.35:
        risk_points += 1
    if debt_ratio is not None and debt_ratio >= 0.65:
        risk_points += 1
    if risk_points == 0:
        return "medium_low"
    if risk_points == 1:
        return "medium"
    if risk_points == 2:
        return "medium_high"
    return "high"


def weighted_score(factor_scores: dict[str, float], weights: dict[str, int]) -> float:
    total_weight = sum(weights.values())
    total = sum(factor_scores[key] * weight for key, weight in weights.items())
    return round(total / total_weight, 2)


def factor_scores_for(
    strategy_type: str, market: dict[str, Any], financial: dict[str, Any]
) -> tuple[dict[str, float], dict[str, int]]:
    pe = metric(market, "pe")
    pb = metric(market, "pb")
    dividend_yield = metric(market, "dividend_yield")
    max_drawdown = metric(market, "max_drawdown_1y")
    roe = metric(financial, "roe")
    roe_stability = metric(financial, "roe_stability")
    debt_ratio = metric(financial, "debt_ratio")
    free_cash_flow = metric(financial, "free_cash_flow")
    revenue_growth = metric(financial, "revenue_growth_3y")
    profit_growth = metric(financial, "net_profit_growth_3y")

    valuation_score = (
        100
        if valuation_level(pe, pb) == "cheap"
        else 75
        if valuation_level(pe, pb) == "fair"
        else 35
    )

    if strategy_type == "dividend_blue_chip":
        weights = {
            "free_cash_flow_quality": 25,
            "dividend_yield": 20,
            "roe_stability": 20,
            "debt_level": 15,
            "valuation_level": 15,
            "growth_quality": 5,
        }
        factors = {
            "free_cash_flow_quality": 85
            if free_cash_flow is not None and free_cash_flow > 0
            else 45,
            "dividend_yield": score_ratio(dividend_yield, 0.0, 0.05),
            "roe_stability": score_ratio(roe_stability, 0.4, 0.95),
            "debt_level": score_ratio(debt_ratio, 0.25, 0.75, inverse=True),
            "valuation_level": valuation_score,
            "growth_quality": score_ratio(profit_growth, -0.05, 0.20),
        }
        return factors, weights

    if strategy_type == "quality_growth":
        weights = {
            "industry_space": 25,
            "competitive_advantage": 25,
            "revenue_growth": 20,
            "roe_level": 15,
            "cash_flow_quality": 10,
            "valuation_level": 5,
        }
        factors = {
            "industry_space": score_ratio(revenue_growth, 0.0, 0.40),
            "competitive_advantage": score_ratio(roe, 0.05, 0.25),
            "revenue_growth": score_ratio(revenue_growth, 0.0, 0.40),
            "roe_level": score_ratio(roe, 0.05, 0.25),
            "cash_flow_quality": 85 if free_cash_flow is not None and free_cash_flow > 0 else 45,
            "valuation_level": valuation_score,
        }
        return factors, weights

    if strategy_type == "cycle_reversal":
        weights = {
            "cycle_position": 30,
            "cost_advantage": 25,
            "resource_reserve": 20,
            "balance_sheet_quality": 15,
            "valuation_level": 10,
        }
        factors = {
            "cycle_position": score_ratio(max_drawdown, 0.0, 0.50),
            "cost_advantage": score_ratio(roe, 0.05, 0.22),
            "resource_reserve": score_ratio(profit_growth, -0.10, 0.30),
            "balance_sheet_quality": score_ratio(debt_ratio, 0.30, 0.75, inverse=True),
            "valuation_level": valuation_score,
        }
        return factors, weights

    if strategy_type == "steady_quality_growth":
        weights = {
            "profitability_quality": 20,
            "growth_durability": 20,
            "roe_stability": 15,
            "cash_flow_quality": 15,
            "balance_sheet_quality": 10,
            "shareholder_return": 10,
            "valuation_level": 10,
        }
        factors = {
            "profitability_quality": score_ratio(roe, 0.08, 0.20),
            "growth_durability": score_ratio(revenue_growth, 0.04, 0.18),
            "roe_stability": score_ratio(roe_stability, 0.50, 0.98),
            "cash_flow_quality": 85 if free_cash_flow is not None and free_cash_flow > 0 else 45,
            "balance_sheet_quality": score_ratio(debt_ratio, 0.25, 0.65, inverse=True),
            "shareholder_return": score_ratio(dividend_yield, 0.0, 0.04),
            "valuation_level": valuation_score,
        }
        return factors, weights

    weights = {
        "tam_potential": 30,
        "technical_barrier": 25,
        "team_quality": 20,
        "commercialization_path": 15,
        "cash_reserve": 10,
    }
    factors = {
        "tam_potential": score_ratio(revenue_growth, 0.0, 0.50),
        "technical_barrier": score_ratio(roe, -0.10, 0.20),
        "team_quality": 50.0,
        "commercialization_path": score_ratio(profit_growth, -0.20, 0.40),
        "cash_reserve": score_ratio(debt_ratio, 0.20, 0.80, inverse=True),
    }
    return factors, weights


def collect_missing_fields(*rows: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        fields.extend(str(field) for field in row.get("missing_fields", []))
    return sorted(set(fields))


def collect_warnings(*rows: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for row in rows:
        warnings.extend(str(warning) for warning in row.get("warnings", []))
    return warnings


def collect_quality_flags(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for row in rows:
        flags.extend(dict(flag) for flag in row.get("quality_flags", []))
    return flags


def collect_field_sources(
    stock: dict[str, Any], market: dict[str, Any], financial: dict[str, Any]
) -> dict[str, Any]:
    return {
        "stock": dict(stock.get("field_sources", {})),
        "market_data": dict(market.get("field_sources", {})),
        "financial_data": dict(financial.get("field_sources", {})),
    }


def collect_provider_conflicts(
    stock: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
) -> dict[str, Any]:
    conflicts: dict[str, Any] = {}
    payloads = (
        ("stock", stock),
        ("market_data", market),
        ("financial_data", financial),
    )
    for payload_type, row in payloads:
        payload_conflicts = row.get("provider_conflicts", {})
        if payload_conflicts:
            conflicts[payload_type] = {
                str(field): dict(conflict) for field, conflict in payload_conflicts.items()
            }
    return conflicts


def data_quality_score(missing: list[str]) -> float:
    return round(max(0.0, 1.0 - len(missing) * 0.08), 2)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _as_of_timestamp(value: str | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return _parse_timestamp(value) or datetime.now(UTC)


def assess_freshness(
    stock: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
    *,
    as_of: str | datetime | None,
    max_market_age_days: int,
    max_financial_age_days: int,
    max_stock_age_days: int,
) -> dict[str, dict[str, Any]]:
    reference = _as_of_timestamp(as_of)
    result: dict[str, dict[str, Any]] = {}
    for key, row, max_age_days in (
        ("stock", stock, max_stock_age_days),
        ("market_data", market, max_market_age_days),
        ("financial_data", financial, max_financial_age_days),
    ):
        updated_at = row.get("updated_at")
        updated = _parse_timestamp(updated_at)
        age_hours = (
            None if updated is None else max(0.0, (reference - updated).total_seconds() / 3600)
        )
        is_stale = updated is None or (age_hours is not None and age_hours > max_age_days * 24)
        result[key] = {
            "updated_at": updated_at,
            "age_hours": None if age_hours is None else round(age_hours, 2),
            "max_age_days": max_age_days,
            "is_stale": is_stale,
        }
    return result


def research_confidence_score(quality: float, freshness: dict[str, dict[str, Any]]) -> float:
    freshness_multiplier = 1.0
    if freshness["market_data"]["is_stale"]:
        freshness_multiplier *= 0.5
    if freshness["financial_data"]["is_stale"]:
        freshness_multiplier *= 0.75
    if freshness["stock"]["is_stale"]:
        freshness_multiplier *= 0.9
    return round(quality * freshness_multiplier * 100, 2)


def split_missing_fields_by_applicability(
    missing: list[str],
    non_applicable_fields: list[str],
) -> tuple[list[str], list[str]]:
    non_applicable = set(non_applicable_fields)
    applicable_missing = sorted(field for field in missing if field not in non_applicable)
    model_not_applicable = sorted(field for field in missing if field in non_applicable)
    return applicable_missing, model_not_applicable


def suggestion_for(
    score: float,
    valuation: str,
    risk: str,
    quality: float,
    *,
    has_stale_data: bool = False,
    requires_review: bool = False,
) -> str:
    """Return a research-priority label, never a trade action."""

    if requires_review or quality < 0.85 or has_stale_data:
        return "needs_review"
    if risk in {"medium_high", "high"}:
        return "risk_review"
    if valuation == "expensive":
        return "defer"
    if score >= 85:
        return "high_research_priority"
    if score >= 75:
        return "research_priority"
    if score >= 65:
        return "monitor"
    return "low_priority"


def resolved_market_cap(stock: dict[str, Any], market: dict[str, Any]) -> float | None:
    market_cap = metric(stock, "market_cap")
    if market_cap is None:
        market_cap = metric(market, "market_cap")
    return market_cap


def score_stock(
    stock: dict[str, Any],
    market: dict[str, Any],
    financial: dict[str, Any],
    *,
    freshness: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    classification = classify_stock(stock, market, financial)
    strategy_ranking = ranked_strategies(classification["classification_fit_scores"])
    primary_strategy = strategy_ranking[0][0]
    secondary_strategy = strategy_ranking[1][0] if len(strategy_ranking) > 1 else primary_strategy
    profile = classify_company_profile(stock, market, financial)
    selected_model = select_scoring_model(profile, primary_strategy)
    if selected_model in SECTOR_MODEL_KEYS:
        factors, weights = factor_scores_for_model(selected_model, market, financial)
    else:
        factors, weights = factor_scores_for(primary_strategy, market, financial)
    score = weighted_score(factors, weights)
    raw_missing = collect_missing_fields(stock, market, financial)
    applicable_missing, _ = split_missing_fields_by_applicability(
        raw_missing,
        list(profile.get("non_applicable_fields", [])),
    )
    fit = model_fit_score(profile, selected_model, raw_missing)
    model_fit_quality_flags = list(fit["quality_flags"])
    provider_quality_flags = collect_quality_flags(stock, market, financial)
    provider_conflicts = collect_provider_conflicts(stock, market, financial)
    combined_quality_flags = provider_quality_flags + model_fit_quality_flags
    warnings = collect_warnings(stock, market, financial)
    quality = data_quality_score(applicable_missing)
    freshness = freshness or {
        key: {
            "updated_at": row.get("updated_at"),
            "age_hours": None,
            "max_age_days": None,
            "is_stale": False,
        }
        for key, row in (("stock", stock), ("market_data", market), ("financial_data", financial))
    }
    stale_payloads = [key for key, detail in freshness.items() if detail["is_stale"]]
    freshness_flags = [
        {
            "field": key,
            "flag": "stale_data",
            "message": f"{key} exceeds its freshness limit",
            "severity": "review",
        }
        for key in stale_payloads
    ]
    confidence = research_confidence_score(quality, freshness)
    pe = metric(market, "pe")
    pb = metric(market, "pb")
    market_cap = resolved_market_cap(stock, market)
    valuation = valuation_level(pe, pb)
    risk = risk_level(
        metric(market, "volatility_1y"),
        metric(market, "max_drawdown_1y"),
        metric(financial, "debt_ratio"),
    )
    needs_review = (
        classification["needs_agent_review"]
        or fit["model_fit_score"] < 0.65
        or bool(stock.get("needs_agent_review"))
        or bool(market.get("needs_agent_review"))
        or (bool(financial.get("needs_agent_review")) and bool(applicable_missing))
        or bool(provider_conflicts)
        or quality < 0.85
        or bool(stale_payloads)
    )
    suggestion = suggestion_for(
        score,
        valuation,
        risk,
        quality,
        has_stale_data=bool(stale_payloads),
        requires_review=needs_review,
    )

    evidence_summary: dict[str, list[str]] = {
        "positive": [],
        "negative": [],
        "risk": [],
    }
    if valuation == "cheap":
        evidence_summary["positive"].append("估值水平为低估")
    if valuation == "expensive":
        evidence_summary["negative"].append("估值水平偏贵")
    if risk in {"medium", "medium_high", "high"}:
        evidence_summary["risk"].append(f"风险等级为 {risk}")
    if model_fit_quality_flags:
        evidence_summary["risk"].append("存在模型不适用字段，已从数据质量扣分中剔除")
    if provider_quality_flags or provider_conflicts:
        evidence_summary["risk"].append("存在上游数据质量标记，需结合字段来源与提供方冲突复核")
    if stale_payloads:
        evidence_summary["risk"].append("存在超过时效门槛的数据，当前结论需要复核")

    return {
        "skill_version": __version__,
        "scoring_version": SCORING_VERSION,
        "schema_version": SCHEMA_VERSION,
        "symbol": stock.get("symbol"),
        "name": stock.get("name"),
        "market": stock.get("market"),
        "industry": stock.get("industry"),
        "market_cap": market_cap,
        "strategy_type": primary_strategy,
        "strategy_label": STRATEGY_LABELS[primary_strategy],
        "classification_confidence": classification["classification_confidence"],
        "classification_fit_scores": classification["classification_fit_scores"],
        "primary_strategy": primary_strategy,
        "primary_strategy_label": STRATEGY_LABELS[primary_strategy],
        "secondary_strategy": secondary_strategy,
        "secondary_strategy_label": STRATEGY_LABELS[secondary_strategy],
        "strategy_confidence": classification["classification_confidence"],
        "selected_model": selected_model,
        "selected_model_label": MODEL_LABELS.get(selected_model, selected_model),
        "applicable_models": list(profile.get("preferred_models", [])),
        "model_fit_score": fit["model_fit_score"],
        "model_fit_label": fit["model_fit_label"],
        "quality_flags": combined_quality_flags + freshness_flags,
        "field_sources": collect_field_sources(stock, market, financial),
        "provider_conflicts": provider_conflicts,
        "company_profile": profile,
        "investment_score": score,
        "business_score": score,
        "research_confidence_score": confidence,
        "research_confidence_label": "高"
        if confidence >= 85
        else ("中" if confidence >= 65 else "低"),
        "freshness": freshness,
        "risk_level": risk,
        "valuation_level": valuation,
        "suggestion": suggestion,
        "suggestion_label": SUGGESTION_LABELS[suggestion],
        "data_quality_score": quality,
        "needs_agent_review": needs_review,
        "source": {
            "stock": stock.get("source", "unknown"),
            "market_data": market.get("source", "unknown"),
            "financial_data": financial.get("source", "unknown"),
        },
        "updated_at": {
            "stock": stock.get("updated_at"),
            "market_data": market.get("updated_at"),
            "financial_data": financial.get("updated_at"),
        },
        "factor_scores": {key: round(value, 2) for key, value in factors.items()},
        "key_metrics": {
            "market_cap": market_cap,
            "pe": pe,
            "pb": pb,
            "dividend_yield": metric(market, "dividend_yield"),
            "roe": metric(financial, "roe"),
            "debt_ratio": metric(financial, "debt_ratio"),
            "revenue_growth_3y": metric(financial, "revenue_growth_3y"),
            "net_profit_growth_3y": metric(financial, "net_profit_growth_3y"),
            "max_drawdown_1y": metric(market, "max_drawdown_1y"),
        },
        "missing_fields": applicable_missing,
        "warnings": warnings + (["数据质量较低，需要复核"] if quality < 0.85 else []),
        "evidence_summary": evidence_summary,
    }


def index_by_symbol(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["symbol"]): item for item in payload.get("items", [])}


def score_pool(
    pool_payload: dict[str, Any],
    market_payload: dict[str, Any],
    financial_payload: dict[str, Any],
    *,
    pool_name: str = "manual",
    as_of: str | datetime | None = None,
    max_market_age_days: int = DEFAULT_MAX_MARKET_AGE_DAYS,
    max_financial_age_days: int = DEFAULT_MAX_FINANCIAL_AGE_DAYS,
    max_stock_age_days: int = DEFAULT_MAX_STOCK_AGE_DAYS,
) -> dict[str, Any]:
    market_by_symbol = index_by_symbol(market_payload)
    financial_by_symbol = index_by_symbol(financial_payload)
    items = []

    for stock in pool_payload.get("items", []):
        symbol = str(stock["symbol"])
        market = market_by_symbol.get(
            symbol,
            {
                "symbol": symbol,
                "source": "missing",
                "missing_fields": ["market_data"],
                "needs_agent_review": True,
            },
        )
        financial = financial_by_symbol.get(
            symbol,
            {
                "symbol": symbol,
                "source": "missing",
                "missing_fields": ["financial_data"],
                "needs_agent_review": True,
            },
        )
        freshness = assess_freshness(
            stock,
            market,
            financial,
            as_of=as_of,
            max_market_age_days=max_market_age_days,
            max_financial_age_days=max_financial_age_days,
            max_stock_age_days=max_stock_age_days,
        )
        items.append(score_stock(stock, market, financial, freshness=freshness))

    items.sort(key=lambda item: item["investment_score"], reverse=True)
    return {
        "metadata": {
            "pool": pool_name,
            "generated_at": utc_now_iso(),
            "skill_version": __version__,
            "data_sources": sorted(
                {
                    str(pool_payload.get("metadata", {}).get("source", "unknown")),
                    str(market_payload.get("metadata", {}).get("source", "unknown")),
                    str(financial_payload.get("metadata", {}).get("source", "unknown")),
                }
            ),
            "scoring_version": SCORING_VERSION,
            "schema_version": SCHEMA_VERSION,
            "freshness_policy": {
                "as_of": _as_of_timestamp(as_of).isoformat(),
                "max_market_age_days": max_market_age_days,
                "max_financial_age_days": max_financial_age_days,
                "max_stock_age_days": max_stock_age_days,
            },
        },
        "items": items,
    }
