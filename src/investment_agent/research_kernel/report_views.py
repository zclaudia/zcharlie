from __future__ import annotations

from typing import Any


def _note(
    text: str,
    *,
    source: str,
    field: str | None = None,
    value: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"source": source, "text": text}
    if field is not None:
        payload["field"] = field
    if value is not None:
        payload["value"] = value
    return payload


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _evidence_notes(
    evidence_summary: dict[str, Any],
    key: str,
    *,
    label: str,
) -> list[dict[str, Any]]:
    return [
        _note(str(value), source=f"evidence_summary.{key}", field=label)
        for value in evidence_summary.get(key, [])
    ]


def _positive_thesis(
    item: dict[str, Any],
    peer_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence = item.get("evidence_summary", {})
    metrics = item.get("key_metrics", {})
    notes = _evidence_notes(evidence, "positive", label="positive")

    investment_score = _number(item.get("investment_score"))
    if investment_score is not None and investment_score >= 80:
        notes.append(
            _note(
                f"投资评分为 {investment_score:g}，处于优先研究区间",
                source="score",
                field="investment_score",
                value=investment_score,
            )
        )

    model_fit_score = _number(item.get("model_fit_score"))
    if model_fit_score is not None and model_fit_score >= 0.8:
        notes.append(
            _note(
                f"模型适配度为 {item.get('model_fit_label', '高')}",
                source="model_fit",
                field="model_fit_score",
                value=model_fit_score,
            )
        )

    roe = _number(metrics.get("roe"))
    if roe is not None and roe >= 0.12:
        notes.append(
            _note(
                f"ROE 为 {roe:.2%}，盈利质量具备支撑",
                source="key_metrics",
                field="roe",
                value=roe,
            )
        )

    revenue_growth = _number(metrics.get("revenue_growth_3y"))
    if revenue_growth is not None and revenue_growth >= 0.10:
        notes.append(
            _note(
                f"三年收入复合增速为 {revenue_growth:.2%}",
                source="key_metrics",
                field="revenue_growth_3y",
                value=revenue_growth,
            )
        )

    stronger = peer_comparison.get("peer_summary", {}).get("stronger_than_peers", [])
    if stronger:
        notes.append(
            _note(
                "相对同行占优指标：" + "、".join(str(metric) for metric in stronger),
                source="peer_comparison",
                field="stronger_than_peers",
                value=list(stronger),
            )
        )

    return notes


def _negative_thesis(
    item: dict[str, Any],
    peer_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence = item.get("evidence_summary", {})
    metrics = item.get("key_metrics", {})
    notes = _evidence_notes(evidence, "negative", label="negative")

    if item.get("valuation_level") == "expensive":
        notes.append(
            _note(
                "估值水平偏贵",
                source="score",
                field="valuation_level",
                value="expensive",
            )
        )

    debt_ratio = _number(metrics.get("debt_ratio"))
    if debt_ratio is not None and debt_ratio >= 0.65:
        notes.append(
            _note(
                f"资产负债率为 {debt_ratio:.2%}，财务杠杆需要跟踪",
                source="key_metrics",
                field="debt_ratio",
                value=debt_ratio,
            )
        )

    max_drawdown = _number(metrics.get("max_drawdown_1y"))
    if max_drawdown is not None and max_drawdown >= 0.30:
        notes.append(
            _note(
                f"近一年最大回撤为 {max_drawdown:.2%}",
                source="key_metrics",
                field="max_drawdown_1y",
                value=max_drawdown,
            )
        )

    weaker = peer_comparison.get("peer_summary", {}).get("weaker_than_peers", [])
    if weaker:
        notes.append(
            _note(
                "相对同行偏弱指标：" + "、".join(str(metric) for metric in weaker),
                source="peer_comparison",
                field="weaker_than_peers",
                value=list(weaker),
            )
        )

    return notes


def _risk_review(
    item: dict[str, Any],
    peer_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    evidence = item.get("evidence_summary", {})
    notes = _evidence_notes(evidence, "risk", label="risk")

    risk_level = item.get("risk_level")
    if risk_level:
        notes.append(
            _note(
                f"风险等级为 {risk_level}",
                source="score",
                field="risk_level",
                value=risk_level,
            )
        )

    missing_fields = item.get("missing_fields", [])
    if missing_fields:
        notes.append(
            _note(
                "存在缺失字段：" + "、".join(str(field) for field in missing_fields),
                source="data_quality",
                field="missing_fields",
                value=list(missing_fields),
            )
        )

    if item.get("needs_agent_review"):
        notes.append(
            _note(
                "当前结果需要 Agent 复核",
                source="data_quality",
                field="needs_agent_review",
                value=True,
            )
        )

    if peer_comparison.get("needs_peer_review"):
        notes.append(
            _note(
                "同行样本不足或同行数据不足，需要复核",
                source="peer_comparison",
                field="needs_peer_review",
                value=True,
            )
        )

    for symbol in peer_comparison.get("manual_peer_missing_symbols", []):
        notes.append(
            _note(
                f"手工同行 {symbol} 未进入比较样本",
                source="peer_comparison",
                field="manual_peer_missing_symbols",
                value=symbol,
            )
        )

    return notes


def _valuation_notes(item: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = item.get("evidence_summary", {})
    metrics = item.get("key_metrics", {})
    notes = _evidence_notes(evidence, "negative", label="valuation")

    valuation_level = item.get("valuation_level", "unknown")
    notes.append(
        _note(
            f"估值水平为 {valuation_level}",
            source="score",
            field="valuation_level",
            value=valuation_level,
        )
    )

    for metric in ("pe", "pb", "dividend_yield"):
        if metric in metrics:
            notes.append(
                _note(
                    f"{metric} 为 {metrics.get(metric)}",
                    source="key_metrics",
                    field=metric,
                    value=metrics.get(metric),
                )
            )

    return notes


def _tracking_indicators(item: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = item.get("key_metrics", {})
    indicator_reasons = {
        "roe": "验证盈利质量是否延续",
        "revenue_growth_3y": "验证成长性是否延续",
        "net_profit_growth_3y": "验证利润增长是否延续",
        "pe": "跟踪估值消化情况",
        "pb": "跟踪资产估值水平",
        "debt_ratio": "跟踪财务杠杆风险",
        "max_drawdown_1y": "跟踪波动与回撤风险",
    }
    indicators: list[dict[str, Any]] = []
    for indicator, reason in indicator_reasons.items():
        if indicator in metrics:
            indicators.append(
                {
                    "indicator": indicator,
                    "current_value": metrics.get(indicator),
                    "reason": reason,
                }
            )
    indicators.append(
        {
            "indicator": "data_quality_score",
            "current_value": item.get("data_quality_score"),
            "reason": "确认数据完整性是否足以支撑结论",
        }
    )
    return indicators


def _summary_text(item: dict[str, Any]) -> str:
    return (
        f"{item.get('name')} 当前评分 {item.get('investment_score')}，"
        f"研究工作流标签为 {item.get('suggestion_label')}，"
        f"风险等级 {item.get('risk_level')}，估值水平 {item.get('valuation_level')}。"
    )


def build_structured_views(
    item: dict[str, Any],
    peer_comparison: dict[str, Any],
) -> dict[str, Any]:
    peer_summary = peer_comparison.get("peer_summary", {})
    return {
        "summary_conclusion": {
            "text": _summary_text(item),
            "investment_score": item.get("investment_score"),
            "suggestion": item.get("suggestion"),
            "suggestion_label": item.get("suggestion_label"),
            "risk_level": item.get("risk_level"),
            "valuation_level": item.get("valuation_level"),
            "needs_agent_review": item.get("needs_agent_review"),
        },
        "positive_thesis": _positive_thesis(item, peer_comparison),
        "negative_thesis": _negative_thesis(item, peer_comparison),
        "valuation_view": {
            "valuation_level": item.get("valuation_level"),
            "metrics": {
                key: item.get("key_metrics", {}).get(key) for key in ("pe", "pb", "dividend_yield")
            },
            "notes": _valuation_notes(item),
        },
        "peer_position": {
            "peer_count": peer_summary.get("peer_count", 0),
            "metrics_with_peer_data": peer_summary.get("metrics_with_peer_data", 0),
            "stronger_than_peers": peer_summary.get("stronger_than_peers", []),
            "weaker_than_peers": peer_summary.get("weaker_than_peers", []),
            "needs_peer_review": peer_comparison.get("needs_peer_review", True),
            "peer_selection_mode": peer_comparison.get(
                "peer_selection_mode",
                "automatic",
            ),
            "manual_peer_missing_symbols": peer_comparison.get(
                "manual_peer_missing_symbols",
                [],
            ),
        },
        "risk_review": _risk_review(item, peer_comparison),
        "follow_up_tracking_indicators": _tracking_indicators(item),
    }


def _judge_stance(item: dict[str, Any]) -> tuple[str, str]:
    score = _number(item.get("investment_score")) or 0
    data_quality_score = _number(item.get("data_quality_score")) or 0
    model_fit_score = _number(item.get("model_fit_score")) or 0
    risk_level = item.get("risk_level")
    valuation_level = item.get("valuation_level")

    if item.get("needs_agent_review") or data_quality_score < 0.8 or model_fit_score < 0.7:
        return "needs_review", "需复核"
    if score >= 80 and risk_level != "high" and valuation_level != "expensive":
        return "constructive", "偏积极"
    if score < 60 or risk_level == "high" or valuation_level == "expensive":
        return "cautious", "偏谨慎"
    return "balanced", "均衡"


def build_evidence_synthesis(
    item: dict[str, Any],
    structured_views: dict[str, Any],
) -> dict[str, Any]:
    bull_case = list(structured_views.get("positive_thesis", []))
    bear_case = list(structured_views.get("negative_thesis", []))
    bear_case.extend(structured_views.get("risk_review", []))
    peer_position = structured_views.get("peer_position", {})
    review_required = bool(item.get("needs_agent_review") or peer_position.get("needs_peer_review"))

    stance, stance_label = _judge_stance(item)
    judge_text = (
        "综合评分、估值、风险、同行位置和数据质量后，"
        f"当前结论为{stance_label}。"
        "该结论用于研究优先级排序，不构成交易建议。"
    )
    return {
        "bull_case": bull_case,
        "bear_case": bear_case,
        "judge_view": {
            "stance": stance,
            "stance_label": stance_label,
            "text": judge_text,
            "support_count": len(bull_case),
            "opposition_count": len(bear_case),
            "review_required": review_required,
        },
    }
