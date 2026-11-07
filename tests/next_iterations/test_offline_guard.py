"""Acceptance probes for fail-closed offline infrastructure."""

import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from next_iterations_offline import OfflineViolationError  # type: ignore[import-not-found]


@pytest.mark.parametrize("action", [
    lambda: socket.getaddrinfo("provider.example.invalid", 443),
    lambda: socket.socket().connect(("127.0.0.1", 9)),
    lambda: httpx.get("https://provider.example.invalid"),
])
def test_real_transports_blocked(action: Callable[[], Any]) -> None:
    with pytest.raises(OfflineViolationError):
        action()


def test_requests_blocked() -> None:
    import requests

    with pytest.raises(OfflineViolationError):
        requests.get("https://provider.example.invalid")


def test_dotenv_and_real_sqlite_blocked(tmp_path: Path) -> None:
    from investment_agent.config import Settings

    assert Settings.model_config["env_file"] is None
    assert Settings().api_key == "next-iterations-inert-api-key"  # type: ignore[call-arg]
    with pytest.raises(OfflineViolationError, match="dotenv"):
        (tmp_path / ".env").read_text()
    with pytest.raises(OfflineViolationError, match="SQLite"):
        sqlite3.connect("/tmp/user-account.db")
    with sqlite3.connect(tmp_path / "temporary.db") as database:
        database.execute("create table probe (value integer)")
        database.execute("insert into probe values (7)")
        assert database.execute("select value from probe").fetchone() == (7,)


async def test_asgi_and_mock_transports_still_work() -> None:
    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"offline": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/probe")).json() == {"offline": True}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as mock_client:
        assert (await mock_client.get("https://fake-provider.invalid")).status_code == 200


def test_subprocess_escape_routes_blocked() -> None:
    with pytest.raises(OfflineViolationError, match="subprocess"):
        subprocess.run(["curl", "https://provider.example.invalid"], check=False)
    with pytest.raises(OfflineViolationError, match="startup bypass"):
        subprocess.run([sys.executable, "-I", "-c", "pass"], check=False)
    with pytest.raises(OfflineViolationError):
        os.system("curl https://provider.example.invalid")


def test_node_child_has_dom_and_no_network(tmp_path: Path) -> None:
    code = """
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { lookup } from 'node:dns';
import { request } from 'node:https';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
test('DOM event interactions', () => {
  document.body.innerHTML = '<button>before</button>';
  const button = document.querySelector('button');
  button.addEventListener('click', () => { button.textContent = 'after'; });
  button.dispatchEvent(new Event('click'));
  assert.equal(button.textContent, 'after');
  const cancellation = new AbortController();
  button.addEventListener('click', () => {}, { signal: cancellation.signal });
  cancellation.abort();
  assert.equal(location.href, window.location.href);
  assert.equal(sessionStorage, window.sessionStorage);
});
test('all real external routes fail', () => {
  for (const action of [() => fetch('https://provider.example.invalid'),
      () => lookup('provider.example.invalid', () => {}),
      () => request('https://provider.example.invalid'),
      () => new window.XMLHttpRequest().open('GET', 'https://provider.example.invalid'),
      () => spawnSync(process.execPath, ['-e', 'fetch("https://provider.example.invalid")']),
      () => readFileSync('.env')]) {
    assert.throws(action, /offline guard blocked/);
  }
});
"""
    test_file = tmp_path / "offline-dom.test.mjs"
    test_file.write_text(code)
    child = subprocess.run(["node", "--test", "--experimental-test-isolation=none", str(test_file)],
                           env={}, capture_output=True, text=True)
    assert child.returncode == 0, child.stdout + child.stderr
    assert "# pass 2" in child.stdout


def test_wrapper_rejects_unimplemented_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "check_next_iterations.py"
    spec = importlib.util.spec_from_file_location("offline_wrapper_probe", wrapper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SCOPES", {"EMPTY-PROBE": ()})
    monkeypatch.setattr(sys, "argv", [str(wrapper), "--scope", "EMPTY-PROBE", "--offline"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert "no reviewed implemented tests" in capsys.readouterr().err


def test_python_startup_import_failure_is_fatal() -> None:
    startup = Path(os.environ["NEXT_ITERATIONS_SANDBOX"]) / "startup"
    shadow = startup / "next_iterations_offline.py"
    shadow.write_text("raise ImportError('deliberate startup failure')\n")
    try:
        child = subprocess.run([sys.executable, "-c", "print('unguarded code executed')"],
                               capture_output=True, text=True)
        assert child.returncode == 86
        assert "unguarded code executed" not in child.stdout
    finally:
        shadow.unlink()


def test_fake_clock_is_explicit_and_adjustable(offline_clock: Any) -> None:
    original = offline_clock.now()
    assert original.isoformat() == "2026-11-07T00:00:00+00:00"
    assert offline_clock.advance(timedelta(seconds=30)) == original + timedelta(seconds=30)


def test_pytest_rejects_zero_selected_tests() -> None:
    root = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "next_iterations_offline",
         "tests/unit/test_next_iterations_guard_probe.py", "-k", "no_matching_probe", "-q"],
        cwd=root, capture_output=True, text=True,
    )
    assert child.returncode != 0
    assert "collected zero tests" in child.stderr


def test_pytest_rejects_only_skipped_tests(tmp_path: Path) -> None:
    probe = tmp_path / "test_skipped.py"
    probe.write_text("import pytest\n@pytest.mark.skip(reason='probe')\ndef test_probe(): pass\n")
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "next_iterations_offline", str(probe), "-q"],
        capture_output=True, text=True,
    )
    assert child.returncode == 5, child.stdout + child.stderr
    assert "zero passing tests" in child.stdout
    assert probe.exists()  # The child must not erase its parent's temp fixture.


@pytest.mark.parametrize("mode, expected", [("pass", 0), ("empty", 5), ("skip", 5)])
def test_wrapper_runs_node_and_rejects_empty_node_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: int,
) -> None:
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "check_next_iterations.py"
    spec = importlib.util.spec_from_file_location("offline_node_wrapper_probe", wrapper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    filename = "component.test.mjs"
    (tmp_path / filename).write_text(
        "import { test } from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        + ({
            "pass": "test('DOM exists', () => assert.equal(document.body.tagName, 'BODY'));\n",
            "skip": "test.skip('skipped probe', () => {});\n",
            "empty": "",
        }[mode])
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "SCOPES", {"NODE-PROBE": (filename,)})
    monkeypatch.setattr(sys, "argv", [str(wrapper), "--scope", "NODE-PROBE", "--offline"])
    assert module.main() == expected
