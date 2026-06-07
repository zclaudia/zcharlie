"""Strategy registry — instantiates concrete StrategyAgent objects from YAML configs."""

from pathlib import Path

from investment_agent.config import LLMTierConfig
from investment_agent.llm.agent_factory import build_agent
from investment_agent.strategies.base import BaseStrategyAgent
from investment_agent.strategies.loader import load_strategy_config

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STRATEGIES_DIR = PROJECT_ROOT / "strategies"
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"


def build_strategy_agent(
    *,
    yaml_path: Path,
    tier2_config: LLMTierConfig,
    tier3_config: LLMTierConfig,
    prompts_root: Path = DEFAULT_PROMPTS_DIR,
) -> BaseStrategyAgent:
    cfg = load_strategy_config(yaml_path)

    tier2_agent = build_agent(
        config=tier2_config,
        system_prompt="",
        output_type=cfg.scoring_schema,
    )
    tier3_agent = build_agent(
        config=tier3_config,
        system_prompt="",
        output_type=cfg.deep_schema,
    )

    class _Concrete(BaseStrategyAgent):
        pass

    _Concrete.config = cfg
    return _Concrete(
        prompts_dir=prompts_root,
        tier2_agent=tier2_agent,
        tier3_agent=tier3_agent,
    )


def load_all_strategies(
    *,
    tier2_config: LLMTierConfig,
    tier3_config: LLMTierConfig,
    strategies_dir: Path = DEFAULT_STRATEGIES_DIR,
    prompts_root: Path = DEFAULT_PROMPTS_DIR,
) -> dict[str, BaseStrategyAgent]:
    out: dict[str, BaseStrategyAgent] = {}
    for yaml_path in sorted(strategies_dir.glob("*.yaml")):
        agent = build_strategy_agent(
            yaml_path=yaml_path,
            tier2_config=tier2_config,
            tier3_config=tier3_config,
            prompts_root=prompts_root,
        )
        out[agent.code] = agent
    return out
