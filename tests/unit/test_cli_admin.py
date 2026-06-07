from typer.testing import CliRunner


def test_admin_cost_help():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["admin", "cost", "--help"])
    assert result.exit_code == 0
    assert "month" in result.output.lower()
