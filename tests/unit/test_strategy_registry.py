from typer.testing import CliRunner


def test_cli_strategy_help() -> None:
    from investment_agent.cli.main import app

    r = CliRunner().invoke(app, ["strategy", "--help"])
    assert r.exit_code == 0
    assert "list" in r.output
    assert "sync" in r.output
    assert "show" in r.output
