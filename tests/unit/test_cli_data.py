from typer.testing import CliRunner


def test_cli_help_lists_data_commands():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "seed" in result.output
    assert "backfill" in result.output
    assert "refresh" in result.output
