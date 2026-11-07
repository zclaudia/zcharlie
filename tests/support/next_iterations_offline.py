"""Fail-closed, opt-in isolation for the next-iterations offline test runner.

Load with ``-p next_iterations_offline`` and NEXT_ITERATIONS_OFFLINE=1. The
wrapper also installs this module through sitecustomize in Python children.
This is an accidental-I/O guard, not a sandbox for hostile test code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "tests" / "support"
BOOTSTRAP = SUPPORT / "next_iterations_dom.mjs"
_INSTALLED = False


class OfflineViolationError(RuntimeError):
    """An offline test attempted access to a real external resource."""


def offline_environment() -> dict[str, str]:
    """Build a child environment without preserving provider credentials."""
    env = dict(os.environ)
    sandbox = Path(env.get("NEXT_ITERATIONS_SANDBOX") or tempfile.mkdtemp(prefix="next-iterations-"))
    sandbox.mkdir(parents=True, exist_ok=True)
    startup = sandbox / "startup"
    startup.mkdir(exist_ok=True)
    (startup / "sitecustomize.py").write_text(
        "import os\ntry:\n import next_iterations_offline\n next_iterations_offline.install()\n"
        "except BaseException:\n os._exit(86)\n"
    )
    # Remove inherited app settings as well as common SDK credential names. The
    # application Settings class can carry endpoints, catalog JSON and tokens.
    from investment_agent.config import Settings

    field_names = {name.upper() for name in Settings.model_fields}
    for key in list(env):
        if key.upper() in field_names or any(
            word in key.upper() for word in ("API_KEY", "TOKEN", "PASSWORD", "SECRET", "PROXY")
        ):
            env.pop(key)
    env.update(
        NEXT_ITERATIONS_OFFLINE="1",
        NEXT_ITERATIONS_SANDBOX=str(sandbox),
        DATA_DIR=str(sandbox / "data"),
        DATABASE_URL=f"sqlite+aiosqlite:///{sandbox / 'offline.db'}",
        API_KEY="next-iterations-inert-api-key",
        OPENAI_API_KEY="next-iterations-inert-provider-key",
        DEEPSEEK_API_KEY="next-iterations-inert-provider-key",
        LLM_TIER1_API_KEY="next-iterations-inert-provider-key",
        LLM_TIER2_API_KEY="next-iterations-inert-provider-key",
        LLM_TIER3_API_KEY="next-iterations-inert-provider-key",
        PYTHONPATH=os.pathsep.join((str(startup), str(SUPPORT), str(ROOT / "src"))),
        NODE_OPTIONS=f"--import={BOOTSTRAP}",
        PYTEST_ADDOPTS="",
    )
    (sandbox / "data").mkdir(exist_ok=True)
    return env


def _refuse(*args: Any, **kwargs: Any) -> Any:
    raise OfflineViolationError("next-iterations offline guard blocked real network/transport")


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if event in {
        "socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg", "socket.bind",
        "os.system", "os.exec", "os.posix_spawn", "os.posix_spawnp",
    }:
        raise OfflineViolationError(f"next-iterations offline guard blocked {event}")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        name = Path(os.fsdecode(args[0])).name
        if name == ".env" or name.startswith(".env."):
            raise OfflineViolationError("next-iterations offline guard blocked dotenv read")
    if event == "sqlite3.connect":
        database = os.fsdecode(args[0])
        if database == ":memory:" or database.startswith("file::memory:"):
            return
        # URI database names can hide a disk path; only conventional temporary
        # SQLite paths are accepted. Tests needing shared memory use :memory:.
        if database.startswith("file:"):
            raise OfflineViolationError("next-iterations offline guard blocked SQLite URI")
        root = Path(os.environ["NEXT_ITERATIONS_SANDBOX"]).resolve()
        if not Path(database).resolve().is_relative_to(root):
            raise OfflineViolationError("next-iterations offline guard blocked non-temporary SQLite")
    if event == "subprocess.Popen":
        executable, argv, _cwd, env = args
        binary = Path(os.fsdecode(executable)).name
        if binary not in {"node", Path(sys.executable).name, "python", "python3"}:
            raise OfflineViolationError(f"next-iterations offline guard blocked subprocess {binary}")
        if binary != "node" and any(arg in {"-I", "-S", "-E"} for arg in argv[1:]):
            raise OfflineViolationError("next-iterations offline guard blocked Python startup bypass")
        if not env or env.get("NEXT_ITERATIONS_OFFLINE") != "1":
            raise OfflineViolationError("next-iterations offline guard blocked unguarded subprocess")


def install() -> None:
    """Install before collection, including imported conftests and test modules."""
    global _INSTALLED
    if _INSTALLED:
        return
    if os.environ.get("NEXT_ITERATIONS_OFFLINE") != "1":
        raise RuntimeError("load offline plugin with NEXT_ITERATIONS_OFFLINE=1 or use wrapper")
    env = offline_environment()
    os.environ.clear()
    os.environ.update(env)
    _INSTALLED = True
    sys.addaudithook(_audit)
    from investment_agent.config import Settings

    Settings.model_config["env_file"] = None
    import dotenv
    import httpx
    from pydantic_settings.sources import DotEnvSettingsSource

    # Third-party SDK settings (not only our Settings) may default to .env.
    # Suppress their source without opening it; explicit dotenv reads still fail.
    DotEnvSettingsSource._read_env_files = lambda self: {}  # type: ignore[method-assign]
    dotenv.load_dotenv = _refuse
    dotenv.dotenv_values = _refuse
    httpx.HTTPTransport.handle_request = _refuse  # type: ignore[method-assign]
    httpx.AsyncHTTPTransport.handle_async_request = _refuse  # type: ignore[method-assign]
    try:
        import requests
    except ImportError:
        pass
    else:
        requests.sessions.Session.send = _refuse  # type: ignore[method-assign]
        requests.sessions.Session.request = _refuse  # type: ignore[method-assign]
    original_popen = subprocess.Popen.__init__
    required = {key: env[key] for key in (
        "NEXT_ITERATIONS_OFFLINE", "NEXT_ITERATIONS_SANDBOX", "PYTHONPATH", "NODE_OPTIONS",
        "DATA_DIR", "DATABASE_URL", "API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
        "LLM_TIER1_API_KEY", "LLM_TIER2_API_KEY", "LLM_TIER3_API_KEY", "PATH",
    )}

    def guarded_popen(self: Any, *args: Any, **kwargs: Any) -> None:
        child_env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
        child_env.update(required)
        kwargs["env"] = child_env
        original_popen(self, *args, **kwargs)

    subprocess.Popen.__init__ = guarded_popen  # type: ignore[method-assign]


def pytest_configure(config: Any) -> None:
    install()
    if config.option.basetemp is None:
        # A nested pytest process must not clean its parent's fixture directory.
        config.option.basetemp = str(
            Path(os.environ["NEXT_ITERATIONS_SANDBOX"]) / f"pytest-{os.getpid()}"
        )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:

    forbidden = [item.nodeid for item in items if item.get_closest_marker("network")]
    if forbidden:
        raise pytest.UsageError("offline allowlist collected network-marked tests: " + ", ".join(forbidden))
    if not items:
        raise pytest.UsageError("offline allowlist collected zero tests")


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if exitstatus == 0 and (reporter is None or not reporter.stats.get("passed")):
        session.exitstatus = pytest.ExitCode.NO_TESTS_COLLECTED
        if reporter is not None:
            reporter.write_line("offline allowlist had zero passing tests", red=True)


@dataclass
class OfflineClock:
    """Explicit deterministic UTC clock for service-level fault/retry tests."""

    current: datetime = datetime(2026, 11, 7, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> datetime:
        self.current += delta
        return self.current


@pytest.fixture
def offline_clock() -> OfflineClock:
    return OfflineClock()


# pytest imports plugins before conftest/collection; sitecustomize imports this
# same module before any child code. Both entry points must fail closed.
if os.environ.get("NEXT_ITERATIONS_OFFLINE") == "1":
    install()
