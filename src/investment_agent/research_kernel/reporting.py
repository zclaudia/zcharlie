from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from investment_agent.research_kernel.models import utc_now_iso
from investment_agent.research_kernel.peer_comparison import build_peer_comparison
from investment_agent.research_kernel.report_views import (
    build_evidence_synthesis,
    build_structured_views,
)


def build_report_questions(item: dict[str, Any]) -> list[str]:
    return [
        f"{item['name']} 的主营业务和收入结构是否支持 {item['strategy_label']} 逻辑？",
        "当前估值与行业和历史区间相比是否合理？",
        "最大风险来自财务质量、行业周期、竞争格局还是估值？",
        "哪些缺失字段需要在进一步研究前补齐？",
    ]


def _format_source(source: dict[str, Any]) -> str:
    ordered = {
        "stock": source.get("stock", "unknown"),
        "market_data": source.get("market_data", "unknown"),
        "financial_data": source.get("financial_data", "unknown"),
    }
    return "；".join(f"{key}:{value}" for key, value in ordered.items())


def _format_updated_at(updated_at: Any) -> str:
    if isinstance(updated_at, dict):
        ordered = {
            "stock": updated_at.get("stock", "unknown"),
            "market_data": updated_at.get("market_data", "unknown"),
            "financial_data": updated_at.get("financial_data", "unknown"),
        }
        return "；".join(f"{key}:{value}" for key, value in ordered.items())
    return str(updated_at)


def _format_peer_names(peer_group: list[dict[str, Any]]) -> str:
    if not peer_group:
        return "无"
    return "、".join(str(peer.get("name") or peer.get("symbol")) for peer in peer_group)


def _format_metric_value(value: Any) -> str:
    if value is None:
        return "null"
    return str(value)


def _format_fraction_as_percent(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value * 100:.2f}%"
    return _format_metric_value(value)


def _format_note_summary(notes: list[dict[str, Any]], *, limit: int = 3) -> str:
    if not notes:
        return "无"
    return "；".join(str(note.get("text", "")) for note in notes[:limit])


def _manual_peer_symbols(
    peer_symbols_by_target: Mapping[str, list[str]] | None,
    symbol: str,
) -> list[str] | None:
    if peer_symbols_by_target is None:
        return None
    return peer_symbols_by_target.get(str(symbol))


def _build_report_item(
    index: int,
    item: dict[str, Any],
    all_items: list[dict[str, Any]],
    peer_symbols_by_target: Mapping[str, list[str]] | None,
) -> dict[str, Any]:
    peer_comparison = build_peer_comparison(
        item,
        all_items,
        manual_peer_symbols=_manual_peer_symbols(
            peer_symbols_by_target,
            item["symbol"],
        ),
    )
    structured_views = build_structured_views(item, peer_comparison)
    evidence_synthesis = build_evidence_synthesis(item, structured_views)
    return {
        "rank": index + 1,
        "skill_version": item.get("skill_version", "unknown"),
        "scoring_version": item.get("scoring_version", "unknown"),
        "schema_version": item.get("schema_version", "unknown"),
        "symbol": item["symbol"],
        "name": item["name"],
        "strategy_type": item["strategy_type"],
        "strategy_label": item["strategy_label"],
        "primary_strategy": item.get("primary_strategy", item["strategy_type"]),
        "primary_strategy_label": item.get("primary_strategy_label", item["strategy_label"]),
        "secondary_strategy": item.get("secondary_strategy", item["strategy_type"]),
        "secondary_strategy_label": item.get("secondary_strategy_label", item["strategy_label"]),
        "selected_model": item.get("selected_model", item["strategy_type"]),
        "selected_model_label": item.get("selected_model_label", item["strategy_label"]),
        "model_fit_score": item.get("model_fit_score", 1.0),
        "model_fit_label": item.get("model_fit_label", "高"),
        "quality_flags": item.get("quality_flags", []),
        "field_sources": item.get("field_sources", {}),
        "provider_conflicts": item.get("provider_conflicts", {}),
        "evidence_summary": item.get("evidence_summary", {}),
        "peer_comparison": peer_comparison,
        "structured_views": structured_views,
        "evidence_synthesis": evidence_synthesis,
        "investment_score": item["investment_score"],
        "business_score": item.get("business_score", item["investment_score"]),
        "research_confidence_score": item.get(
            "research_confidence_score", item["data_quality_score"] * 100
        ),
        "research_confidence_label": item.get("research_confidence_label", "高"),
        "freshness": item.get("freshness", {}),
        "risk_level": item["risk_level"],
        "valuation_level": item["valuation_level"],
        "suggestion": item["suggestion"],
        "suggestion_label": item["suggestion_label"],
        "company_profile": item.get("company_profile", {}),
        "data_quality_score": item["data_quality_score"],
        "needs_agent_review": item["needs_agent_review"],
        "source": item["source"],
        "updated_at": item["updated_at"],
        "factor_scores": item["factor_scores"],
        "key_metrics": item["key_metrics"],
        "missing_fields": item["missing_fields"],
        "warnings": item["warnings"],
        "report_questions": build_report_questions(item),
    }


def build_report_context(
    scores_payload: dict[str, Any],
    *,
    top: int,
    peer_symbols_by_target: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    selected_items = scores_payload.get("items", [])[:top]
    all_items = scores_payload.get("items", [])
    metadata = scores_payload.get("metadata", {})

    return {
        "metadata": {
            "pool": metadata.get("pool", "unknown"),
            "generated_at": utc_now_iso(),
            "skill_version": metadata.get("skill_version", "unknown"),
            "scoring_version": metadata.get("scoring_version", "unknown"),
            "schema_version": metadata.get("schema_version", "unknown"),
            "top": top,
        },
        "items": [
            _build_report_item(index, item, all_items, peer_symbols_by_target)
            for index, item in enumerate(selected_items)
        ],
    }


def _format_research_source(source: Any) -> str:
    if not isinstance(source, dict):
        return "来源等级：unknown；日期：unknown；URL：unknown"
    return (
        f"来源等级：{source.get('source_level', 'unknown')}；"
        f"日期：{source.get('published_at', 'unknown')}；"
        f"URL：{source.get('source_url', 'unknown')}"
    )


def _append_research_evidence_sections(lines: list[str], evidence: dict[str, Any]) -> None:
    annual_report = evidence.get("annual_report", {})
    lines.extend(["", "### 年报业务拆解", ""])
    if isinstance(annual_report, dict):
        lines.append(
            f"- 报告年度：{annual_report.get('report_year', 'unknown')}；"
            f"{_format_research_source(annual_report.get('source'))}"
        )
        segments = annual_report.get("segments", [])
        if isinstance(segments, list) and segments:
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                metrics = [
                    f"收入：{_format_metric_value(segment.get('revenue'))}",
                    f"同比：{_format_fraction_as_percent(segment.get('revenue_yoy'))}",
                    f"毛利率：{_format_fraction_as_percent(segment.get('gross_margin'))}",
                ]
                lines.append(
                    f"- {segment.get('name', '未命名分部')}；"
                    f"{'；'.join(metrics)}；"
                    f"定位：{segment.get('page_or_section', 'unknown')}；"
                    f"提取质量：{segment.get('extraction_quality', 'unknown')}；"
                    f"{_format_research_source(segment.get('source'))}"
                )
        else:
            lines.append("- 待人工补充业务分部数据。")
    else:
        lines.append("- 待人工补充年报数据。")

    lines.extend(["", "### 行业与政策证据", ""])
    policies = evidence.get("policy_evidence", [])
    if isinstance(policies, list) and policies:
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            effective_until = policy.get("effective_until")
            effective_period = (
                f"有效期：{effective_until}；" if effective_until not in (None, "") else ""
            )
            lines.append(
                f"- {policy.get('name', '未命名政策')}；发文机构：{policy.get('issuer', 'unknown')}；"
                f"日期：{policy.get('published_at', 'unknown')}；"
                f"{effective_period}"
                f"赛道：{', '.join(str(sector) for sector in policy.get('sectors', [])) or '未标注'}；"
                f"{_format_research_source(policy.get('source'))}"
            )
    else:
        lines.append("- 待人工补充政策证据。")

    lines.extend(["", "### 影响传导链", ""])
    chains = evidence.get("impact_chain", [])
    if isinstance(chains, list) and chains:
        for chain in chains:
            if not isinstance(chain, dict):
                continue
            lines.append(
                "- 研究推论："
                f"{chain.get('policy', 'unknown')} → {chain.get('industry_demand', 'unknown')} → "
                f"{chain.get('company_business', 'unknown')} → {chain.get('tracking_indicator', 'unknown')}；"
                f"{_format_research_source(chain.get('source'))}"
            )
    else:
        lines.append("- 待人工补充影响传导链。")

    lines.extend(["", "### 关键跟踪指标", ""])
    indicators = evidence.get("tracking_indicators", [])
    if isinstance(indicators, list) and indicators:
        for indicator in indicators:
            if not isinstance(indicator, dict):
                continue
            lines.append(
                f"- {indicator.get('name', '未命名指标')}：当前值：{_format_metric_value(indicator.get('current_value'))}；"
                f"验证条件：{indicator.get('verification_condition', 'unknown')}；"
                f"下次观察：{indicator.get('next_review_at', 'unknown')}；"
                f"{_format_research_source(indicator.get('source'))}"
            )
    else:
        lines.append("- 待人工补充跟踪指标。")

    lines.extend(["", "### 研究证据质量", ""])
    needs_review = bool(evidence.get("needs_agent_review"))
    lines.append(f"- 需要人工复核：{'是' if needs_review else '否'}")
    flags = evidence.get("quality_flags", [])
    if isinstance(flags, list) and flags:
        for flag in flags:
            if isinstance(flag, dict):
                lines.append(
                    f"- {flag.get('field', 'unknown')}: {flag.get('flag', 'unknown')} - "
                    f"{flag.get('message', '')}"
                )
    elif needs_review:
        lines.append("- 待人工确认复核原因。")
    else:
        lines.append("- 无")


def render_report_markdown(context_payload: dict[str, Any]) -> str:
    lines: list[str] = [
        "# 股票研究上下文",
        "",
        f"- 股票池：{context_payload['metadata']['pool']}",
        f"- Top N：{context_payload['metadata']['top']}",
        f"- 生成时间：{context_payload['metadata']['generated_at']}",
        f"- Skill 版本：{context_payload['metadata'].get('skill_version', 'unknown')}",
        f"- 评分版本：{context_payload['metadata'].get('scoring_version', 'unknown')}",
        f"- Schema 版本：{context_payload['metadata'].get('schema_version', 'unknown')}",
        "",
    ]

    for item in context_payload.get("items", []):
        lines.extend(
            [
                f"## {item['rank']}. {item['name']}（{item['symbol']}）",
                "",
                f"- 策略类型：{item['strategy_label']}",
                f"- 主策略：{item.get('primary_strategy_label', item['strategy_label'])}",
                f"- 副策略：{item.get('secondary_strategy_label', item['strategy_label'])}",
                f"- 选用模型：{item.get('selected_model_label', item['strategy_label'])}",
                f"- 模型适配度：{item.get('model_fit_label', '高')}（{item.get('model_fit_score', 1.0)}）",
                f"- 投资评分：{item['investment_score']}",
                f"- 业务评分：{item.get('business_score', item['investment_score'])}",
                f"- 研究置信度：{item.get('research_confidence_label', '高')}（{item.get('research_confidence_score', item['data_quality_score'] * 100)}）",
                f"- 风险等级：{item['risk_level']}",
                f"- 估值水平：{item['valuation_level']}",
                f"- 研究工作流：{item['suggestion_label']}",
                f"- 数据来源：{_format_source(item['source'])}",
                f"- 数据更新时间：{_format_updated_at(item['updated_at'])}",
                f"- 数据质量：{item['data_quality_score']}",
                f"- 时效状态：{item.get('freshness', {})}",
                f"- 需要 Agent 复核：{item['needs_agent_review']}",
                f"- 缺失字段：{', '.join(item['missing_fields']) if item['missing_fields'] else '无'}",
                "",
            ]
        )

        structured_views = item.get("structured_views", {})
        if structured_views:
            summary = structured_views.get("summary_conclusion", {})
            valuation_view = structured_views.get("valuation_view", {})
            valuation_metrics = valuation_view.get("metrics", {})
            peer_position = structured_views.get("peer_position", {})
            lines.extend(
                [
                    "### 结构化报告视图",
                    "",
                    f"- 总结：{summary.get('text', '无')}",
                    (
                        "- 正面逻辑："
                        f"{_format_note_summary(structured_views.get('positive_thesis', []))}"
                    ),
                    (
                        "- 负面逻辑："
                        f"{_format_note_summary(structured_views.get('negative_thesis', []))}"
                    ),
                    (
                        f"- 估值视角：{valuation_view.get('valuation_level', 'unknown')}；"
                        f"pe={_format_metric_value(valuation_metrics.get('pe'))}；"
                        f"pb={_format_metric_value(valuation_metrics.get('pb'))}；"
                        "dividend_yield="
                        f"{_format_metric_value(valuation_metrics.get('dividend_yield'))}"
                    ),
                    (
                        f"- 同行位置：同行数量 {peer_position.get('peer_count', 0)}；"
                        "占优 "
                        f"{', '.join(peer_position.get('stronger_than_peers', [])) or '无'}；"
                        f"偏弱 {', '.join(peer_position.get('weaker_than_peers', [])) or '无'}"
                    ),
                    (
                        "- 风险复核："
                        f"{_format_note_summary(structured_views.get('risk_review', []))}"
                    ),
                ]
            )
            for indicator in structured_views.get("follow_up_tracking_indicators", []):
                lines.append(
                    f"- 跟踪指标：{indicator.get('indicator')}="
                    f"{_format_metric_value(indicator.get('current_value'))}；"
                    f"{indicator.get('reason')}"
                )
            lines.append("")

        lines.extend(
            [
                "### 关键指标",
                "",
            ]
        )
        for key, value in item["key_metrics"].items():
            lines.append(f"- {key}: {value}")

        lines.extend(["", "### 子项评分", ""])
        for key, value in item["factor_scores"].items():
            lines.append(f"- {key}: {value}")

        research_evidence = item.get("research_evidence")
        if isinstance(research_evidence, dict):
            _append_research_evidence_sections(lines, research_evidence)

        if "peer_comparison" in item:
            peer_comparison = item.get("peer_comparison", {})
            peer_summary = peer_comparison.get("peer_summary", {})
            lines.extend(["", "### 同行比较", ""])
            lines.append(f"- 同行：{_format_peer_names(peer_comparison.get('peer_group', []))}")
            lines.append(f"- 同行数量：{peer_summary.get('peer_count', 0)}")
            lines.append(f"- 需要同行复核：{peer_comparison.get('needs_peer_review', True)}")
            peer_selection_mode = peer_comparison.get("peer_selection_mode")
            if peer_selection_mode == "manual":
                lines.append(f"- 同行选择：{peer_selection_mode}")
                missing_manual_peers = peer_comparison.get("manual_peer_missing_symbols", [])
                if missing_manual_peers:
                    lines.append(f"- 手工同行缺失：{', '.join(missing_manual_peers)}")
            for metric, values in peer_comparison.get("metric_comparison", {}).items():
                lines.append(
                    f"- {metric}: target={_format_metric_value(values.get('target'))}; "
                    f"peer_median={_format_metric_value(values.get('peer_median'))}; "
                    f"relative_position={values.get('relative_position')}"
                )

        lines.extend(["", "### 证据摘要", ""])
        evidence = item.get("evidence_summary", {})
        for key, label in (("positive", "正面"), ("negative", "负面"), ("risk", "风险")):
            values = evidence.get(key, [])
            if values:
                for value in values:
                    lines.append(f"- {label}: {value}")
        if not any(evidence.get(key) for key in ("positive", "negative", "risk")):
            lines.append("- 无")

        synthesis = item.get("evidence_synthesis", {})
        if synthesis:
            judge_view = synthesis.get("judge_view", {})
            lines.extend(["", "### Bull/Bear/Judge 证据综合", ""])
            lines.append(f"- Bull: {_format_note_summary(synthesis.get('bull_case', []))}")
            lines.append(f"- Bear: {_format_note_summary(synthesis.get('bear_case', []))}")
            lines.append(
                f"- Judge: {judge_view.get('stance_label', '未知')} - {judge_view.get('text', '')}"
            )

        lines.extend(["", "### 数据质量标记", ""])
        quality_flags = item.get("quality_flags", [])
        if not quality_flags:
            lines.append("- 无")
        for flag in quality_flags:
            lines.append(f"- {flag.get('field')}: {flag.get('flag')} - {flag.get('message', '')}")

        lines.extend(["", "### 后续研究问题", ""])
        for question in item["report_questions"]:
            lines.append(f"- {question}")

        if item["warnings"]:
            lines.extend(["", "### 风险提示", ""])
            for warning in item["warnings"]:
                lines.append(f"- {warning}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
