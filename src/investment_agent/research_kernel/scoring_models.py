from __future__ import annotations

from typing import Any

from investment_agent.research_kernel.models import MODEL_FIT_LABELS, normalize_number

SECTOR_MODEL_KEYS = {"insurance", "banking", "public_utility", "manufacturing_growth"}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def metric(row: dict[str, Any], key: str) -> float | None:
    try:
        return normalize_number(row.get(key))
    except ValueError:
        return None


# A factor value of ``None`` means "no data": it is excluded from the weighted
# score (remaining weights are renormalised) and reported in
# ``missing_factors``; it is never imputed as a neutral 50 (scoring v1.3).
FactorScores = dict[str, float | None]


def score_ratio(
    value: float | None, low: float, high: float, *, inverse: bool = False
) -> float | None:
    if value is None:
        return None
    if high == low:
        return 50.0
    normalized = (value - low) / (high - low)
    if inverse:
        normalized = 1 - normalized
    return clamp(normalized * 100)


def _positive_or_none(value: float | None) -> float | None:
    """Valuation multiples are only meaningful when positive."""

    return value if value is not None and value > 0 else None


def valuation_level(pe: float | None, pb: float | None) -> str:
    """Bucket valuation from PE/PB; non-positive multiples never count as cheap.

    A non-positive PE (loss-making) or PB (negative equity) is treated as
    unavailable, so it falls back to the conservative placeholder instead of
    passing the ``<= 15`` / ``<= 2`` cheap thresholds.
    """

    pe = _positive_or_none(pe)
    pb = _positive_or_none(pb)
    if pe is None and pb is None:
        return "unknown"
    pe_value = pe if pe is not None else 100.0
    pb_value = pb if pb is not None else 10.0
    if pe_value <= 15 and pb_value <= 2.0:
        return "cheap"
    if pe_value <= 30 and pb_value <= 5.0:
        return "fair"
    return "expensive"


VALUATION_LEVEL_SCORES = {"cheap": 100.0, "fair": 75.0, "expensive": 35.0}


def valuation_factor_score(pe: float | None, pb: float | None) -> float | None:
    """Factor score for the valuation bucket; ``None`` when valuation is unknown."""

    return VALUATION_LEVEL_SCORES.get(valuation_level(pe, pb))


def weighted_score(factor_scores: FactorScores, weights: dict[str, int]) -> float | None:
    """Weighted mean over the factors that have data, weights renormalised.

    Returns ``None`` when no weighted factor has data.
    """

    present = {
        key: weight
        for key, weight in weights.items()
        if weight > 0 and factor_scores.get(key) is not None
    }
    total_weight = sum(present.values())
    if total_weight <= 0:
        return None
    total = sum(float(factor_scores[key] or 0.0) * weight for key, weight in present.items())
    return round(total / total_weight, 2)


def factor_coverage(
    factor_scores: FactorScores, weights: dict[str, int]
) -> tuple[float, list[str]]:
    """Share of model weight backed by data, plus the factors that had none."""

    total_weight = sum(weights.values())
    missing = sorted(key for key in weights if factor_scores.get(key) is None)
    if total_weight <= 0:
        return 0.0, missing
    covered = sum(weight for key, weight in weights.items() if key not in missing)
    return round(covered / total_weight, 4), missing


def ranked_strategies(classification_fit_scores: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(classification_fit_scores.items(), key=lambda item: item[1], reverse=True)


def select_scoring_model(profile: dict[str, Any], primary_strategy: str) -> str:
    preferred = list(profile.get("preferred_models", []))
    if preferred and preferred[0] != "generic":
        return str(preferred[0])
    return primary_strategy


def _fit_label(score: float) -> str:
    if score >= 0.85:
        return MODEL_FIT_LABELS["high"]
    if score >= 0.65:
        return MODEL_FIT_LABELS["medium"]
    return MODEL_FIT_LABELS["low"]


def model_fit_score(
    profile: dict[str, Any],
    selected_model: str,
    missing_fields: list[str],
) -> dict[str, Any]:
    preferred = list(profile.get("preferred_models", []))
    non_applicable = set(str(field) for field in profile.get("non_applicable_fields", []))
    applicable_missing = sorted(field for field in missing_fields if field not in non_applicable)
    model_not_applicable_flags = [
        {"field": field, "flag": "model_not_applicable"}
        for field in sorted(set(missing_fields).intersection(non_applicable))
    ]

    score = 0.75
    if selected_model in preferred[:1]:
        score = 1.0
    elif selected_model in preferred:
        score = 0.85
    elif preferred and preferred[0] != "generic":
        score = 0.55

    return {
        "model_fit_score": score,
        "model_fit_label": _fit_label(score),
        "quality_flags": model_not_applicable_flags,
        "applicable_missing_fields": applicable_missing,
    }


def _valuation_score(market: dict[str, Any]) -> float | None:
    return valuation_factor_score(metric(market, "pe"), metric(market, "pb"))


def _cash_flow_score(financial: dict[str, Any]) -> float | None:
    free_cash_flow = metric(financial, "free_cash_flow")
    operating_cash_flow = metric(financial, "operating_cash_flow")
    if free_cash_flow is not None and free_cash_flow > 0:
        return 85.0
    if operating_cash_flow is not None and operating_cash_flow > 0:
        return 65.0
    if free_cash_flow is None and operating_cash_flow is None:
        return None
    return 45.0


def _insurance_scores(
    market: dict[str, Any], financial: dict[str, Any]
) -> tuple[FactorScores, dict[str, int]]:
    weights = {
        "valuation_level": 20,
        "shareholder_return": 25,
        "roe_quality": 20,
        "profit_growth": 15,
        "drawdown_control": 10,
        "financial_leverage_review": 10,
    }
    factors: FactorScores = {
        "valuation_level": _valuation_score(market),
        "shareholder_return": score_ratio(metric(market, "dividend_yield"), 0.0, 0.06),
        "roe_quality": score_ratio(metric(financial, "roe"), 0.06, 0.18),
        "profit_growth": score_ratio(metric(financial, "net_profit_growth_3y"), -0.05, 0.15),
        "drawdown_control": score_ratio(
            metric(market, "max_drawdown_1y"), 0.10, 0.40, inverse=True
        ),
        "financial_leverage_review": 60.0,
    }
    return factors, weights


def _banking_scores(
    market: dict[str, Any], financial: dict[str, Any]
) -> tuple[FactorScores, dict[str, int]]:
    weights = {
        "valuation_level": 20,
        "shareholder_return": 25,
        "roe_quality": 20,
        "profit_growth": 15,
        "drawdown_control": 10,
        "capital_structure_review": 10,
    }
    factors: FactorScores = {
        "valuation_level": _valuation_score(market),
        "shareholder_return": score_ratio(metric(market, "dividend_yield"), 0.0, 0.06),
        "roe_quality": score_ratio(metric(financial, "roe"), 0.06, 0.18),
        "profit_growth": score_ratio(metric(financial, "net_profit_growth_3y"), -0.05, 0.15),
        "drawdown_control": score_ratio(
            metric(market, "max_drawdown_1y"), 0.10, 0.40, inverse=True
        ),
        "capital_structure_review": 60.0,
    }
    return factors, weights


def _public_utility_scores(
    market: dict[str, Any], financial: dict[str, Any]
) -> tuple[FactorScores, dict[str, int]]:
    weights = {
        "cash_flow_quality": 25,
        "shareholder_return": 20,
        "roe_stability": 15,
        "debt_level": 15,
        "valuation_level": 15,
        "growth_quality": 10,
    }
    factors: FactorScores = {
        "cash_flow_quality": _cash_flow_score(financial),
        "shareholder_return": score_ratio(metric(market, "dividend_yield"), 0.0, 0.05),
        "roe_stability": score_ratio(metric(financial, "roe_stability"), 0.4, 0.95),
        "debt_level": score_ratio(metric(financial, "debt_ratio"), 0.25, 0.75, inverse=True),
        "valuation_level": _valuation_score(market),
        "growth_quality": score_ratio(metric(financial, "net_profit_growth_3y"), -0.05, 0.15),
    }
    return factors, weights


def _manufacturing_growth_scores(
    market: dict[str, Any], financial: dict[str, Any]
) -> tuple[FactorScores, dict[str, int]]:
    weights = {
        "profitability_quality": 20,
        "growth_durability": 20,
        "profit_growth": 20,
        "cash_flow_quality": 15,
        "balance_sheet_quality": 10,
        "valuation_level": 15,
    }
    factors: FactorScores = {
        "profitability_quality": score_ratio(metric(financial, "roe"), 0.08, 0.22),
        "growth_durability": score_ratio(metric(financial, "revenue_growth_3y"), 0.04, 0.22),
        "profit_growth": score_ratio(metric(financial, "net_profit_growth_3y"), 0.04, 0.24),
        "cash_flow_quality": _cash_flow_score(financial),
        "balance_sheet_quality": score_ratio(
            metric(financial, "debt_ratio"), 0.25, 0.65, inverse=True
        ),
        "valuation_level": _valuation_score(market),
    }
    return factors, weights


def factor_scores_for_model(
    model_key: str,
    market: dict[str, Any],
    financial: dict[str, Any],
) -> tuple[FactorScores, dict[str, int]]:
    if model_key == "insurance":
        return _insurance_scores(market, financial)
    if model_key == "banking":
        return _banking_scores(market, financial)
    if model_key == "public_utility":
        return _public_utility_scores(market, financial)
    if model_key == "manufacturing_growth":
        return _manufacturing_growth_scores(market, financial)
    raise ValueError(f"Unsupported sector scoring model: {model_key}")
