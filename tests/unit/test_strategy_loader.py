import pytest
import yaml

from investment_agent.strategies.loader import (
    load_strategy_config,
    sync_strategies_to_db,
)
from investment_agent.strategies.schemas import ValueStockScore


def test_load_strategy_config_value(tmp_path):
    p = tmp_path / "value.yaml"
    p.write_text(yaml.dump({
        "code": "value", "version": "v1.0", "name": "Value Investor",
        "description": "test",
        "screening_rule": {
            "required": [{"metric": "pe", "op": "<", "value": 25}],
            "excluded": [],
            "preferred": [],
        },
        "scoring_prompt": "strategies/value/scoring",
        "deep_prompt": "strategies/value/deep",
    }))
    cfg = load_strategy_config(p)
    assert cfg.code == "value"
    assert cfg.scoring_schema is ValueStockScore
    assert cfg.screening_rule.required[0].metric == "pe"


def test_unknown_code_raises(tmp_path):
    p = tmp_path / "unknown.yaml"
    p.write_text(yaml.dump({
        "code": "ghost", "version": "v1.0", "name": "x", "description": "x",
        "screening_rule": {"required": [], "excluded": [], "preferred": []},
        "scoring_prompt": "a", "deep_prompt": "b",
    }))
    with pytest.raises(ValueError, match="no registered schemas"):
        load_strategy_config(p)


@pytest.mark.asyncio
async def test_sync_strategies_returns_count(tmp_path, db_session):
    """Sync invokes its own session_scope; verify return count only.

    Depends on db_session to initialize the in-memory engine + schema.
    DB row verification deferred to Task 7 CLI test.
    """
    s_dir = tmp_path / "strategies"
    s_dir.mkdir()
    (s_dir / "value.yaml").write_text(yaml.dump({
        "code": "value", "version": "v1.0", "name": "Value", "description": "x",
        "screening_rule": {"required": [], "excluded": [], "preferred": []},
        "scoring_prompt": "a", "deep_prompt": "b",
    }))
    n = await sync_strategies_to_db(s_dir)
    assert n == 1
