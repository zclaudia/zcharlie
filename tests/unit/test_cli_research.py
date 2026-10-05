from typer.testing import CliRunner


def test_cli_research_help():
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["research", "--help"])
    assert result.exit_code == 0
    assert "ifind" in result.output


def test_cli_ifind_exposes_idempotency_key():
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["research", "ifind", "--help"])

    assert result.exit_code == 0
    assert "--idempotency-key" in result.output
