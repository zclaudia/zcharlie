import asyncio
from pathlib import Path

import pytest
import yaml

from investment_agent.liveness import LIVENESS_ENV, liveness

REPO = Path(__file__).resolve().parents[2]


def _entries(path: Path) -> list[Path]:
    return list(path.iterdir())


async def test_liveness_refreshes_file_and_removes_it_on_exit(tmp_path: Path) -> None:
    target = tmp_path / "worker.alive"
    async with liveness(str(target), interval=0.01):
        await asyncio.sleep(0.05)
        assert target.is_file()
        first = target.stat().st_mtime_ns
        await asyncio.sleep(0.05)
        assert target.stat().st_mtime_ns >= first
    assert not target.exists()


async def test_liveness_reads_env_and_is_inert_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LIVENESS_ENV, raising=False)
    async with liveness():
        await asyncio.sleep(0.01)
    assert _entries(tmp_path) == []

    target = tmp_path / "scheduler.alive"
    monkeypatch.setenv(LIVENESS_ENV, str(target))
    async with liveness(interval=0.01):
        await asyncio.sleep(0.03)
        assert target.is_file()


async def test_liveness_survives_an_unwritable_path(tmp_path: Path) -> None:
    target = tmp_path / "missing-dir" / "x.alive"
    async with liveness(str(target), interval=0.01):
        await asyncio.sleep(0.03)
    assert not target.exists()


def test_compose_runs_migrations_first_and_health_checks_every_service() -> None:
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    migrate = services["migrate"]
    assert migrate["command"] == ["ia", "migrate", "up"]
    assert migrate["restart"] == "no"

    for name in ("api", "worker", "scheduler"):
        service = services[name]
        assert service["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
        assert "healthcheck" in service, name
    for name in ("worker", "scheduler"):
        service = services[name]
        assert service["depends_on"]["api"]["condition"] == "service_healthy"
        assert service["environment"][LIVENESS_ENV].startswith("/tmp/")
