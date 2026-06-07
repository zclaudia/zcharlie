from typer.testing import CliRunner


def test_cli_attribute_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["attribute", "--help"])
    assert r.exit_code == 0
    assert "run" in r.output
    assert "show" in r.output
