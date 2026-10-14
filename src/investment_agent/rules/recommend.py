"""Drafting-template suggestion for a manual observation entry.

The watchlist add form asks this module once per stock selection when the user
leaves the template choice on "AI recommendation" (plan 1.5 模板未确定). The
suggestion is advisory only: the entry is stored ``needs_template`` when the
call fails, picks nothing usable or no stored template applies to the stock's
industry, and the user can always override the choice before submitting.

Contract: pick exactly one ``template_id`` among the stored templates filtered
by the stock's industry group (when that is known); the reason is rendered to
the user, so trading-instruction wording blanks it (engineering baseline 3).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dt_time
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.config import Settings
from investment_agent.domain.protocols import LLMClient
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.llm.glossary import readability_rules
from investment_agent.llm.openai_compatible import OpenAICompatibleClient
from investment_agent.rules.draft_policy import trading_terms
from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.store import RuleStore

log = structlog.get_logger(__name__)

SUGGEST_PURPOSE = "rule_template_suggest"
MAX_REASON_CHARS = 300
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_MAX_ENTRIES = 256
DEFAULT_DAILY_LIMIT = 200
"""Cost control for the advisory suggestion (plan 6.2 WP9 费用控制): one call
per stock selection is cached for the TTL, and the day's logged calls cap it."""

_CACHE: dict[tuple[Any, ...], tuple[float, TemplateSuggestion]] = {}


def clear_suggestion_cache() -> None:
    _CACHE.clear()


def _cache_key(
    ticker: str, as_of: date, group: str | None, templates: list[RuleTemplate]
) -> tuple[Any, ...]:
    return (ticker, as_of.isoformat(), group, tuple(template.ref for template in templates))


def _shanghai_day_start(now: datetime | None = None) -> datetime:
    """The UTC instant where the current Beijing day began (``now`` override
    for tests). The advisory cap follows the A-share business date, so the
    window rolls at 08:00 UTC - not at the server's UTC midnight."""

    moment = now or datetime.now(UTC)
    day = moment.astimezone(SHANGHAI_TZ).date()
    return datetime.combine(day, dt_time.min, tzinfo=SHANGHAI_TZ).astimezone(UTC)


async def _suggestions_today(session: AsyncSession, *, now: datetime | None = None) -> int:
    from investment_agent.storage.models.ops import LLMCall

    start = _shanghai_day_start(now)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(LLMCall)
            .where(LLMCall.purpose == SUGGEST_PURPOSE, LLMCall.started_at >= start)
        )
        or 0
    )

ClientFactory = Callable[[AsyncSession], LLMClient]


class TemplateChoice(BaseModel):
    """The suggestion LLM's output."""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


@dataclass(frozen=True)
class TemplateSuggestion:
    template_id: str
    template_version: int
    name: str
    reason: str
    model: str | None = None


_SYSTEM_PROMPT = """你是个人投资研究系统的规则模板推荐器。

给定一只 A 股的基本情况（行业等）和系统中可用的规则模板列表，选出**最合适的一个**模板用于为这只股票起草候选规则。

要求：
- template_id 必须来自给定列表，只选一个；没有合适的就选最接近的，不做组合。
- reason 用简体中文，不超过 80 字，说明为什么这个模板适合（引用行业与模板的适用条件/投资逻辑），不得使用买卖、加减仓、止损、目标价、仓位建议等交易指令语言。
- 只输出 JSON 对象：{"template_id": "...", "reason": "..."}。
"""


def system_prompt() -> str:
    return _SYSTEM_PROMPT + "\n\n" + readability_rules()


def default_client_factory(settings: Settings) -> ClientFactory:
    def build(session: AsyncSession) -> LLMClient:
        return OpenAICompatibleClient(
            tier_name="tier3", config=settings.llm_rule_draft, session=session
        )

    return build


def template_summary(template: RuleTemplate) -> dict[str, Any]:
    """The template card the suggestion LLM sees (authoring notes included)."""

    return {
        key: value
        for key, value in {
            "id": template.id,
            "version": template.version,
            "name": template.name,
            "description": template.description,
            "calibration_status": template.calibration_status,
            "applicable_groups": list(template.applicable_groups),
            "thesis": template.thesis,
            "fits": template.fits,
            "avoids": template.avoids,
            "risks": template.risks,
            "horizon": template.horizon,
        }.items()
        if value not in (None, "", [])
    }


def build_message(
    *,
    ticker: str,
    as_of: date,
    industry_group: str | None,
    templates: list[RuleTemplate],
) -> str:
    return json.dumps(
        {
            "task": "rule_template_suggest",
            "stock": {
                "ticker": ticker,
                "as_of": as_of.isoformat(),
                "industry_group": industry_group,
            },
            "templates": [template_summary(template) for template in templates],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def suggest_template(
    session: AsyncSession,
    *,
    ticker: str,
    as_of: date,
    settings: Settings,
    client_factory: ClientFactory | None = None,
    daily_limit: int = DEFAULT_DAILY_LIMIT,
    use_cache: bool = True,
) -> TemplateSuggestion | None:
    """One logged LLM call; ``None`` whenever no suggestion can be made.

    Advisory and cost-controlled (plan 6.2 WP9): an identical selection within
    the TTL reuses the cached suggestion, and the day's logged suggestion calls
    cap further ones (``None`` = fall back to ``needs_template``).
    """

    templates = await RuleStore(session).latest_templates()
    if not templates:
        return None
    try:
        view = await load_snapshot_view(session, ticker, as_of)
    except LookupError:
        log.info("rule_suggest.unknown_ticker", ticker=ticker)
        return None
    group = view.industry_group or None
    if group is not None:
        candidates = [t for t in templates if group in t.applicable_groups]
        if not candidates:
            log.info("rule_suggest.no_applicable_template", ticker=ticker, group=group)
            return None
    else:
        candidates = list(templates)
    key = _cache_key(ticker, as_of, group, candidates)
    if use_cache:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] > time.monotonic():
            log.info("rule_suggest.cache_hit", ticker=ticker)
            return cached[1]
    if await _suggestions_today(session) >= daily_limit:
        log.warning("rule_suggest.daily_limit", ticker=ticker, limit=daily_limit)
        return None
    factory = client_factory or default_client_factory(settings)
    try:
        result = await factory(session).complete(
            system_prompt=system_prompt(),
            user_message=build_message(
                ticker=ticker, as_of=as_of, industry_group=group, templates=candidates
            ),
            output_model=TemplateChoice,
            purpose=SUGGEST_PURPOSE,
            pipeline_run_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - the suggestion is best effort
        log.warning("rule_suggest.failed", ticker=ticker, error=f"{type(exc).__name__}: {exc}")
        return None
    choice = result.output
    if not isinstance(choice, TemplateChoice):
        log.warning("rule_suggest.malformed", ticker=ticker)
        return None
    picked = next((t for t in candidates if t.id == choice.template_id), None)
    if picked is None:
        log.warning("rule_suggest.unknown_pick", ticker=ticker, picked=choice.template_id)
        return None
    reason = choice.reason.strip()
    found = trading_terms(reason)
    if found:
        log.warning("rule_suggest.trading_language", ticker=ticker, terms=found[:5])
        reason = ""
    log.info(
        "rule_suggest.picked",
        ticker=ticker,
        template_ref=picked.ref,
        model=result.model,
    )
    suggestion = TemplateSuggestion(
        template_id=picked.id,
        template_version=picked.version,
        name=picked.name,
        reason=reason,
        model=result.model,
    )
    if use_cache:
        if len(_CACHE) >= CACHE_MAX_ENTRIES:
            _CACHE.clear()
        _CACHE[key] = (time.monotonic() + CACHE_TTL_SECONDS, suggestion)
    return suggestion


__all__ = [
    "SUGGEST_PURPOSE",
    "clear_suggestion_cache",
    "ClientFactory",
    "TemplateChoice",
    "TemplateSuggestion",
    "build_message",
    "default_client_factory",
    "suggest_template",
    "system_prompt",
    "template_summary",
]
