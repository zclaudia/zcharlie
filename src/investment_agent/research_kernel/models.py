from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

STRATEGY_LABELS: dict[str, str] = {
    "dividend_blue_chip": "红利蓝筹",
    "quality_growth": "质量成长",
    "steady_quality_growth": "稳健质量成长",
    "cycle_reversal": "周期反转",
    "high_growth_venture": "高成长风险投资",
}

SUGGESTION_LABELS: dict[str, str] = {
    "high_research_priority": "高研究优先级",
    "research_priority": "研究优先",
    "monitor": "持续跟踪",
    "defer": "暂缓研究",
    "risk_review": "风险复核",
    "low_priority": "低优先级",
    "needs_review": "需要复核",
    # Legacy labels remain renderable for existing v1 payloads, but v2 scoring
    # never emits them because they can be mistaken for trading instructions.
    "build_position": "建仓（旧版）",
    "observe": "观察（旧版）",
    "watch": "跟踪（旧版）",
    "wait": "等待（旧版）",
    "reduce_or_avoid": "减仓/回避（旧版）",
    "avoid": "回避（旧版）",
}

RISK_LABELS: dict[str, str] = {
    "low": "低",
    "medium_low": "中低",
    "medium": "中",
    "medium_high": "中高",
    "high": "高",
}

VALUATION_LABELS: dict[str, str] = {
    "cheap": "低估",
    "fair": "合理",
    "expensive": "偏贵",
    "unknown": "未知",
}


MODEL_LABELS: dict[str, str] = {
    "generic": "通用模型",
    "dividend_blue_chip": "红利蓝筹模型",
    "quality_growth": "质量成长模型",
    "steady_quality_growth": "稳健质量成长模型",
    "cycle_reversal": "周期反转模型",
    "high_growth_venture": "高成长风险投资模型",
    "insurance": "保险模型",
    "banking": "银行模型",
    "public_utility": "公用事业模型",
    "manufacturing_growth": "制造成长模型",
}

MODEL_FIT_LABELS: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

SECTOR_LABELS: dict[str, str] = {
    "insurance": "保险",
    "banking": "银行",
    "public_utility": "公用事业",
    "manufacturing_growth": "制造成长",
    "general": "通用",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.lower() in {"", "--", "-", "n/a", "nan", "none", "inf", "+inf", "-inf"}:
            return None

        def _to_float(raw_value: str) -> float | None:
            try:
                number = float(raw_value)
            except ValueError:
                return None
            return number if math.isfinite(number) else None

        if cleaned.endswith("%"):
            raw_number = cleaned[:-1].strip()
            parsed = _to_float(raw_number)
            return None if parsed is None else parsed / 100
        value_number = _to_float(cleaned)
        if value_number is None:
            return None
        return value_number if math.isfinite(value_number) else None
    return None


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not math.isfinite(value)
    if isinstance(value, str):
        return value.strip().lower() in {"", "--", "-", "n/a", "nan", "none", "inf", "+inf", "-inf"}
    return False


def missing_fields(mapping: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    return [field for field in fields if field not in mapping or is_missing(mapping[field])]
