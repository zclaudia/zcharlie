"""Actual OS termination/restart boundaries; no upstream model quality claim."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
CHILD = ROOT / "tests/integration/agent_process_fixture.py"


def spawn(tmp_path, mode, port=None):
    (tmp_path / "ag20-isolated").touch()
    env = {**os.environ, "API_KEY": "ag20-inert", "LLM_TIER3_API_KEY": "inert", "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'process.db'}", "DATA_DIR": str(tmp_path), "INVESTOR_AGENT_ENABLED": "true", "INVESTOR_AGENT_BACKGROUND_ENABLED": "false", "AGENT_API_KEY": "inert", "AGENT_BASE_URL": "https://fixture.invalid/v1", "AGENT_MODEL": "fixture"}
    log = (tmp_path / f"{mode}.log").open("w")
    return subprocess.Popen([sys.executable, str(CHILD), mode, str(tmp_path), *([str(port)] if port else [])], cwd=ROOT, env=env, stdout=log, stderr=log)


def until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("child boundary timeout")


def stop(child):
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


def test_os_sigkill_after_publication_and_notification_commit_replays_once(tmp_path):
    seed = spawn(tmp_path, "seed")
    assert seed.wait(timeout=15) == 0, (tmp_path / "seed.log").read_text()
    child = spawn(tmp_path, "crash")
    try:
        until(lambda: (tmp_path / "committed.json").exists() or child.poll() is not None)
        assert (tmp_path / "committed.json").exists(), (tmp_path / "crash.log").read_text()
    finally:
        stop(child)
    assert child.returncode < 0  # actual OS signal, rather than a simulated exception
    committed = json.loads((tmp_path / "committed.json").read_text())
    time.sleep(0.15)
    replacement = spawn(tmp_path, "recover")
    try:
        assert replacement.wait(timeout=15) == 0, (tmp_path / "recover.log").read_text()
    finally:
        stop(replacement)
    result = json.loads((tmp_path / "recovered.json").read_text())
    assert (tmp_path / "lease-fenced").exists()
    assert result["operation"] == committed["operation"]
    assert result["attempt"] == committed["attempt"] + 1
    assert result["investor_publication_event"] == result["investor_publication_outbox"] == result["investor_notification"] == 1
    with sqlite3.connect(tmp_path / "process.db") as db:
        assert db.execute("select count(*) from agent_tool_action where state='committed'").fetchone()[0] == 1
        attempts = db.execute("select status from agent_task_attempt order by lease_attempt").fetchall()
        assert attempts[-1] == ("succeeded",) and ("abandoned",) in attempts


def test_real_http_commit_lost_ack_restart_exact_body_and_disabled_replay(tmp_path):
    seed = spawn(tmp_path, "seed")
    assert seed.wait(timeout=15) == 0, (tmp_path / "seed.log").read_text()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    headers = {"X-API-Key": "ag20-inert"}
    child = spawn(tmp_path, "api-crash", port)
    def ready():
        try:
            return httpx.get(url + "/healthz", timeout=0.2).status_code == 200
        except httpx.HTTPError:
            return False
    try:
        until(ready)
        conversation = httpx.post(url + "/agent/conversations", headers=headers, json={}, timeout=5)
        assert conversation.status_code == 201
        path = f"/agent/conversations/{conversation.json()['id']}/messages"
        body = {"client_message_id": str(uuid4()), "text": "exact lost acknowledgement", "context_refs": []}
        try:
            httpx.post(url + path, headers=headers, json=body, timeout=5)
            raise AssertionError("ack unexpectedly delivered")
        except httpx.HTTPError:
            pass
        assert child.wait(timeout=5) == 86
        assert (tmp_path / "api-committed").exists()
    finally:
        stop(child)
    # Deactivate new admission in this isolated store; committed receipts remain available.
    with sqlite3.connect(tmp_path / "process.db") as db:
        db.execute("update app_setting set value_json='false' where key='investor_agent_enabled'")
    child = spawn(tmp_path, "api-recover", port)
    try:
        until(ready)
        first = httpx.post(url + path, headers=headers, json=body, timeout=5)
        assert first.status_code == 202, first.text
        assert httpx.post(url + path, headers=headers, json=body, timeout=5).json() == first.json()
        assert httpx.post(url + path, headers=headers, json={**body, "text": "changed"}, timeout=5).status_code == 409
        denied = httpx.post(url + path, headers=headers, json={**body, "client_message_id": str(uuid4())}, timeout=5)
        assert denied.status_code == 409, denied.text
        assert httpx.get(url + "/healthz", timeout=5).status_code == 200
    finally:
        stop(child)
    with sqlite3.connect(tmp_path / "process.db") as db:
        assert db.execute("select count(*) from agent_message where client_message_id is not null").fetchone()[0] == 1
