import sys
from types import SimpleNamespace

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


def test_cli_worker_help():
    from investment_agent.cli.main import app

    r = CliRunner().invoke(app, ["worker", "start", "--help"])
    assert r.exit_code == 0
    assert "--once" in r.output


def test_cli_migrate_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["migrate", "--help"])
    assert r.exit_code == 0
    assert "up" in r.output
    assert "down" in r.output
    assert "current" in r.output
    assert "history" in r.output


def test_migrate_uses_active_python_environment(monkeypatch):
    from investment_agent.cli.commands import migrate

    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> SimpleNamespace:
        assert check is False
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)

    migrate._run(["current"])

    assert commands == [[sys.executable, "-m", "alembic", "current"]]
