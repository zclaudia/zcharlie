"""A sibling-directory probe: guarding only next_iterations/ cannot pass this."""

import socket

import pytest

# Deliberately at collection time, before autouse fixtures can install a guard.
with pytest.raises(RuntimeError, match="offline guard blocked"):
    socket.getaddrinfo("provider.example.invalid", 443)


def test_unit_network_probe() -> None:
    with socket.socket() as connection, pytest.raises(RuntimeError, match="offline guard blocked"):
        connection.connect(("127.0.0.1", 9))
