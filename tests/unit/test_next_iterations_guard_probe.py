"""A sibling-directory probe: guarding only next_iterations/ cannot pass this.

The import-time DNS check runs when the shared offline plugin is already
loaded (``-p next_iterations_offline``). The default CI command does not load
that plugin; an unguarded ``getaddrinfo`` raises ``gaierror`` and would abort
the whole unit shard. Skip the probe in that case. The offline runner still
collects and executes it.
"""

import socket
import sys

import pytest

_GUARDED = "next_iterations_offline" in sys.modules

# Deliberately at collection time, before autouse fixtures can install a guard.
if _GUARDED:
    with pytest.raises(RuntimeError, match="offline guard blocked"):
        socket.getaddrinfo("provider.example.invalid", 443)


def test_unit_network_probe() -> None:
    if not _GUARDED:
        pytest.skip("offline guard is not loaded")
    with socket.socket() as connection, pytest.raises(RuntimeError, match="offline guard blocked"):
        connection.connect(("127.0.0.1", 9))
