"""BaseStrategyAgent: concrete strategy = config-driven subclass."""

from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent

from investment_agent.domain.strategy_types import (
    DeepAnalysis,
    DeepAnalysisContext,
    ListingSnapshot,
    ScoringContext,
    StockScore,
)
from investment_agent.strategies.prompt_loader import PromptLoader
from investment_agent.strategies.screening_rule import (
    ScreeningResult,
    ScreeningRule,
    evaluate_rule,
)


@dataclass
class StrategyConfig:
    code: str
    version: str
    name: str
    description: str
    screening_rule: ScreeningRule
    scoring_prompt_template: str
    deep_prompt_template: str
    scoring_schema: type[StockScore]
    deep_schema: type[DeepAnalysis]


class BaseStrategyAgent:
    """All concrete strategies inherit this. Subclasses just set `config`."""

    config: StrategyConfig

    def __init__(self, prompts_dir: Path, tier2_agent: Agent, tier3_agent: Agent) -> None:
        self._prompts = PromptLoader(prompts_dir)
        self._tier2_agent = tier2_agent
        self._tier3_agent = tier3_agent

    @property
    def code(self) -> str:
        return self.config.code

    @property
    def version(self) -> str:
        return self.config.version

    async def screen(
        self, candidates: list[ListingSnapshot]
    ) -> list[ListingSnapshot]:
        out: list[ListingSnapshot] = []
        for snap in candidates:
            result: ScreeningResult = evaluate_rule(self.config.screening_rule, snap)
            if result.passed:
                out.append(snap)
        return out

    async def score(
        self, listing: ListingSnapshot, context: ScoringContext
    ) -> StockScore:
        prompt = self._prompts.render(
            self.config.scoring_prompt_template,
            listing=listing.model_dump(),
            context=context.model_dump(),
        )
        result = await self._tier2_agent.run(prompt)
        score = result.output
        assert isinstance(score, self.config.scoring_schema)
        return score

    async def deep_analyze(
        self, listing: ListingSnapshot, context: DeepAnalysisContext
    ) -> DeepAnalysis:
        prompt = self._prompts.render(
            self.config.deep_prompt_template,
            listing=listing.model_dump(),
            context=context.model_dump(),
        )
        result = await self._tier3_agent.run(prompt)
        analysis = result.output
        assert isinstance(analysis, self.config.deep_schema)
        return analysis
