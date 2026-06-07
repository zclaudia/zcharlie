from typer.testing import CliRunner


def test_pipeline_help_lists_subcommands():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "discover" in result.output
    assert "list" in result.output
    assert "show" in result.output


def test_pipeline_discover_help():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["pipeline", "discover", "--help"])
    assert result.exit_code == 0
    assert "universe" in result.output.lower()
    assert "as-of" in result.output.lower() or "as_of" in result.output.lower()
