from typer.testing import CliRunner


def test_cli_api_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["api", "--help"])
    assert r.exit_code == 0
    assert "serve" in r.output


def test_cli_scheduler_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["scheduler", "--help"])
    assert r.exit_code == 0
    assert "start" in r.output


def test_cli_migrate_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["migrate", "--help"])
    assert r.exit_code == 0
    assert "up" in r.output
    assert "down" in r.output
    assert "current" in r.output
    assert "history" in r.output
