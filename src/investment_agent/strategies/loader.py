"""Load strategies/*.yaml → sync to DB → build concrete StrategyAgent instances."""

from pathlib import Path
from typing import cast

import yaml

from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.strategy import StrategyRepository
from investment_agent.strategies.base import StrategyConfig
from investment_agent.strategies.schemas import SCHEMAS_BY_CODE
from investment_agent.strategies.screening_rule import Condition, ScreeningRule


def _condition_from_yaml(raw: dict[str, object]) -> Condition:
    metric = cast(str, raw["metric"])
    op = cast(str, raw["op"])
    val: object = raw["value"]
    value: float | tuple[float, float]
    if isinstance(val, list):
        if len(val) != 2:
            raise ValueError(
                f"between/tuple value must have exactly 2 elements, got {val!r}"
            )
        value = (float(val[0]), float(val[1]))
    else:
        value = float(cast(float, val))
    return Condition(metric=metric, op=op, value=value)


def _rule_from_yaml(raw: dict[str, object]) -> ScreeningRule:
    required = cast(list[dict[str, object]], raw.get("required", []))
    excluded = cast(list[dict[str, object]], raw.get("excluded", []))
    preferred = cast(list[dict[str, object]], raw.get("preferred", []))
    return ScreeningRule(
        required=[_condition_from_yaml(c) for c in required],
        excluded=[_condition_from_yaml(c) for c in excluded],
        preferred=[_condition_from_yaml(c) for c in preferred],
    )


def load_strategy_config(yaml_path: Path) -> StrategyConfig:
    with yaml_path.open() as f:
        raw = cast(dict[str, object], yaml.safe_load(f))
    code = cast(str, raw["code"])
    if code not in SCHEMAS_BY_CODE:
        raise ValueError(
            f"Strategy code '{code}' has no registered schemas in schemas.py"
        )
    scoring_schema, deep_schema = SCHEMAS_BY_CODE[code]
    return StrategyConfig(
        code=code,
        version=cast(str, raw["version"]),
        name=cast(str, raw["name"]),
        description=cast(str, raw.get("description", "")),
        screening_rule=_rule_from_yaml(cast(dict[str, object], raw["screening_rule"])),
        scoring_prompt_template=cast(str, raw["scoring_prompt"]),
        deep_prompt_template=cast(str, raw["deep_prompt"]),
        scoring_schema=scoring_schema,
        deep_schema=deep_schema,
    )


async def sync_strategies_to_db(strategies_dir: Path) -> int:
    """Read all *.yaml in strategies_dir, upsert into strategy table."""
    n = 0
    # Strategy files are read synchronously at startup; using anyio.Path here
    # would add a heavy dep boundary for no practical gain (these are tiny
    # local YAML files loaded once on boot / sync CLI invocation).
    yaml_paths = sorted(strategies_dir.glob("*.yaml"))  # noqa: ASYNC240
    async with session_scope() as session:
        repo = StrategyRepository(session)
        for yaml_path in yaml_paths:
            with yaml_path.open() as f:  # noqa: ASYNC230
                raw = cast(dict[str, object], yaml.safe_load(f))
            await repo.upsert(
                code=cast(str, raw["code"]),
                name=cast(str, raw["name"]),
                version=cast(str, raw["version"]),
                description=cast("str | None", raw.get("description")),
                config_json=raw,
                enabled=True,
            )
            n += 1
    return n
