"""A second sibling directory proves transport and child isolation."""

import os
import subprocess
import sys

import httpx
import pytest


def test_integration_transport_probe() -> None:
    with httpx.Client() as client, pytest.raises(RuntimeError, match="offline guard blocked"):
        client.get("https://provider.example.invalid")


def test_python_child_cannot_drop_guard_environment() -> None:
    code = """
import os, socket, sqlite3
assert os.environ['API_KEY'] == 'next-iterations-inert-api-key'
for action in [lambda: socket.getaddrinfo('provider.example.invalid', 443),
               lambda: socket.socket().connect(('127.0.0.1', 9)),
               lambda: sqlite3.connect('/tmp/real-account.db')]:
    try:
        action()
    except RuntimeError as error:
        assert 'offline guard blocked' in str(error)
    else:
        raise AssertionError('unguarded child')
print('child guarded')
"""
    child = subprocess.run([sys.executable, "-c", code], env={}, capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "child guarded"
    assert os.environ["NEXT_ITERATIONS_OFFLINE"] == "1"
